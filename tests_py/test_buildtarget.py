# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Where a build runs and how it is executed (``buildtarget.py``).

Two axes rather than one name, and this file is about the seam that
carries them: :func:`~mcuhome.workbench.buildmethods.build_firmware`
takes a target, :func:`~mcuhome.workbench.buildmethods.run_build` takes a
method name and translates it, and both reach the same composition with
the same arguments.

Nothing here builds anything. Every composition is stubbed at its own
backend seam, exactly as in ``test_buildmethods.py`` — what those
compositions do is asserted there and in ``test_localbuild.py``. The
properties asserted here are the ones the two-axis vocabulary is *for*:

* :class:`~mcuhome.workbench.buildtarget.LocalBuild` carries an
  execution and :class:`~mcuhome.workbench.buildtarget.RemoteBuild`
  carries none — a client does not tell somebody else's machine whether
  to start a container, and a field that let it would be the whole
  asymmetry gone;
* a target is **authoritative**: what a caller states on it is what runs,
  whatever the request's method-shaped fields happen to say;
* every method name resolves to the target that describes it, so a
  caller that migrates from the one entry point to the other keeps the
  build it had.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from conftest import EXAMPLES_DIR, resolve_file
from mcuhome.compiler import devbuild
from mcuhome.model.manifest import MANIFEST_FILE

from mcuhome.workbench import buildmethods, buildtarget, containerbuild, sessionclient
from mcuhome.workbench import orchestrator as lb
from mcuhome.workbench.buildlock import holder_of


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


def _build(request: buildmethods.BuildRequest, target) -> buildmethods.BuildOutcome:
    """What a caller does at its entry point: one ``asyncio.run``."""
    return asyncio.run(buildmethods.build_firmware(request, target=target))


def _local_result(tmp_path, seen: dict):
    """A stand-in for the container composition that records its arguments."""

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        outcome = lb.LocalOutcome(
            action="build",
            context_id="sha256:" + "1" * 64,
            exit_code=0,
            status="success",
            successful=True,
            artifacts=(),
            out=tmp_path / "delivery",
        )
        return containerbuild.LocalBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            image="ghcr.io/mcu-home/build-container:test",
        )

    return fake


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------


def test_a_local_build_defaults_to_a_container() -> None:
    """What a caller that stated no execution gets, and why it is that one.

    It is the execution that needs a container runtime and nothing else
    of a toolchain, so it is the only one this package can offer without
    knowing anything about the caller's machine.
    """
    assert isinstance(buildtarget.LocalBuild().execution, buildtarget.ContainerExecution)


def test_a_remote_build_carries_no_execution() -> None:
    """The asymmetry, pinned as a field set rather than as prose.

    A client may say *where* a build runs; it may not tell somebody
    else's machine *how* to run it — that machine's operator configured
    that. A build server answers a context by constructing a target of
    its own, which is also why the multi-hop case needs no special code:
    a server configured to pass work on constructs a ``RemoteBuild``.
    """
    fields = {field.name for field in dataclasses.fields(buildtarget.RemoteBuild)}
    assert "execution" not in fields
    assert fields == {"server", "token", "wait", "max_wait_seconds"}


# --------------------------------------------------------------------------
# A method name is one word for two decisions
# --------------------------------------------------------------------------


def test_the_local_method_is_a_local_build_in_a_container(model, tmp_path) -> None:
    request = buildmethods.BuildRequest(
        model=model,
        out_dir=tmp_path,
        image="ghcr.io/mcu-home/build-container:test",
        ccache_dir=tmp_path / "ccache",
    )
    target = buildmethods.target_for_method(buildmethods.LOCAL, request)
    assert target == buildtarget.LocalBuild(
        execution=buildtarget.ContainerExecution(
            image="ghcr.io/mcu-home/build-container:test", ccache_dir=tmp_path / "ccache"
        )
    )


def test_the_local_dev_method_is_a_local_build_in_a_workspace(model, tmp_path) -> None:
    request = buildmethods.BuildRequest(
        model=model,
        out_dir=tmp_path,
        bootloader_key=tmp_path / "signing.pub",
        module_dir=tmp_path / "module",
        started_in=tmp_path,
        snippets=("debug-rtt",),
    )
    target = buildmethods.target_for_method(buildmethods.LOCAL_DEV, request)
    assert isinstance(target, buildtarget.LocalBuild)
    execution = target.execution
    assert isinstance(execution, buildtarget.WorkspaceExecution)
    assert execution.bootloader_key == tmp_path / "signing.pub"
    assert execution.module_dir == tmp_path / "module"
    assert execution.started_in == tmp_path
    assert execution.snippets == ("debug-rtt",)


def test_the_remote_method_is_a_remote_build(model, tmp_path) -> None:
    request = buildmethods.BuildRequest(
        model=model,
        out_dir=tmp_path,
        server="attic:8100",
        token="a-token",
        wait_for_turn=False,
        max_wait_seconds=90.0,
    )
    target = buildmethods.target_for_method(buildmethods.REMOTE, request)
    assert target == buildtarget.RemoteBuild(
        server="attic:8100", token="a-token", wait=False, max_wait_seconds=90.0
    )


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_preference_is_the_container_target(model, tmp_path, nothing) -> None:
    """The default survives the translation: no name still means a container."""
    request = buildmethods.BuildRequest(model=model, out_dir=tmp_path)
    assert buildmethods.target_for_method(nothing, request) == buildtarget.LocalBuild()


def test_an_unknown_method_refuses_at_the_translation(model, tmp_path) -> None:
    """The same refusal as before, one step earlier — not a ``KeyError``."""
    request = buildmethods.BuildRequest(model=model, out_dir=tmp_path)
    with pytest.raises(buildmethods.UnknownMethod):
        buildmethods.target_for_method("cloud", request)


# --------------------------------------------------------------------------
# Reaching a composition through the seam
# --------------------------------------------------------------------------


def test_a_stated_target_beats_the_requests_method_fields(model, tmp_path, monkeypatch) -> None:
    """A caller that builds a target itself is the one that decides.

    ``BuildRequest`` still carries the method-shaped fields for the name
    entry point, and this is what keeps them from becoming a second
    source of truth: the seam reads the target, and the request's copies
    are ignored — which is what makes deleting them later a deletion
    rather than a rewrite.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(buildmethods, "compose_local_build", _local_result(tmp_path, seen))
    request = buildmethods.BuildRequest(
        model=model,
        out_dir=tmp_path,
        image="ghcr.io/mcu-home/build-container:from-the-request",
        ccache_dir=tmp_path / "from-the-request",
    )
    outcome = _build(
        request,
        buildtarget.LocalBuild(
            execution=buildtarget.ContainerExecution(
                image="ghcr.io/mcu-home/build-container:from-the-target",
                ccache_dir=tmp_path / "from-the-target",
            )
        ),
    )
    assert outcome.successful
    assert seen["image"] == "ghcr.io/mcu-home/build-container:from-the-target"
    assert seen["ccache_dir"] == tmp_path / "from-the-target"


def test_a_workspace_target_reaches_the_host_build(model, tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        return devbuild.DevBuildResult(
            plan=None, log="", images=[], merged=None, manifest_path=tmp_path / MANIFEST_FILE
        )

    monkeypatch.setattr(devbuild, "run_dev_build", fake)
    outcome = _build(
        buildmethods.BuildRequest(model=model, out_dir=tmp_path),
        buildtarget.LocalBuild(
            execution=buildtarget.WorkspaceExecution(
                bootloader_key=tmp_path / "signing.pub",
                module_dir=tmp_path / "module",
                started_in=tmp_path,
                snippets=("debug-rtt",),
            )
        ),
    )
    assert outcome.method == buildmethods.LOCAL_DEV
    assert seen["bootloader_key"] == tmp_path / "signing.pub"
    assert seen["snippets"] == ("debug-rtt",)


def test_a_remote_target_reaches_the_session_client(model, tmp_path, monkeypatch) -> None:
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
            successful=True,
            artifacts=(),
            out=tmp_path / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    outcome = _build(
        buildmethods.BuildRequest(model=model, out_dir=tmp_path, context_dir=context),
        buildtarget.RemoteBuild(
            server="build.example:8080", token="a-token", wait=False, max_wait_seconds=90.0
        ),
    )
    assert outcome.method == buildmethods.REMOTE
    assert seen["url"] == "ws://build.example:8080/ws"
    assert seen["token"] == "a-token"
    assert seen["wait"] is False
    assert seen["max_wait"] == 90.0


def test_the_name_entry_point_and_the_seam_run_the_same_build(model, tmp_path, monkeypatch) -> None:
    """``run_build`` is the translation and nothing else.

    The one property that makes the older entry point safe to keep while
    callers migrate: whatever a method name meant, it still means, and it
    reaches the composition with the arguments it always did.
    """
    request = buildmethods.BuildRequest(
        model=model,
        out_dir=tmp_path,
        image="ghcr.io/mcu-home/build-container:test",
        ccache_dir=tmp_path / "ccache",
        jobs=3,
    )

    by_name: dict[str, object] = {}
    monkeypatch.setattr(buildmethods, "compose_local_build", _local_result(tmp_path, by_name))
    assert asyncio.run(buildmethods.run_build(request, method=buildmethods.LOCAL)).successful

    by_target: dict[str, object] = {}
    monkeypatch.setattr(buildmethods, "compose_local_build", _local_result(tmp_path, by_target))
    assert _build(request, buildmethods.target_for_method(buildmethods.LOCAL, request)).successful

    assert by_name == by_target


# --------------------------------------------------------------------------
# What the seam refuses
# --------------------------------------------------------------------------


def test_a_target_this_package_does_not_run_is_a_type_error(model, tmp_path) -> None:
    """A name can be mistyped; an object cannot.

    So an unimplemented target is a programming mistake and says so,
    rather than borrowing the wording of a refusal a user could act on.
    """
    request = buildmethods.BuildRequest(model=model, out_dir=tmp_path)
    with pytest.raises(TypeError, match="BuildTarget"):
        _build(request, buildtarget.BuildTarget())
    with pytest.raises(TypeError, match="Execution"):
        _build(request, buildtarget.LocalBuild(execution=buildtarget.Execution()))


def test_the_seam_holds_the_build_directory(model, tmp_path, monkeypatch) -> None:
    """The guard is at the seam, so both entry points inherit it.

    ``run_build`` used to take the lock itself; a caller that reaches the
    seam directly — a build server, an embedder — would have had none.
    """
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen["holder"] = holder_of(tmp_path)
        return devbuild.DevBuildResult(
            plan=None, log="", images=[], merged=None, manifest_path=tmp_path / MANIFEST_FILE
        )

    monkeypatch.setattr(devbuild, "run_dev_build", fake)
    outcome = _build(
        buildmethods.BuildRequest(model=model, out_dir=tmp_path),
        buildtarget.LocalBuild(
            execution=buildtarget.WorkspaceExecution(
                bootloader_key=tmp_path / "signing.pub",
                module_dir=tmp_path / "module",
                started_in=tmp_path,
            )
        ),
    )
    assert outcome.successful
    holder = seen["holder"]
    assert holder is not None
    assert holder["device"] == model.device.name
    assert holder["operation"] == "build"
