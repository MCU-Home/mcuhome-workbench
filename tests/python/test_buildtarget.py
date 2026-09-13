# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Where a build runs and how it is executed (``buildtarget.py``).

Two axes rather than one name, and this file is about the seam that
carries them: :func:`~mcuhome.workbench.build.build_firmware` takes a
target object, a target name, or nothing at all, and whichever of the
three it was given reaches the same composition with the same
arguments.

Nothing here builds anything. Every composition is stubbed at its own
backend seam, exactly as in ``test_build.py`` — what those
compositions do is asserted there and in ``test_localbuild.py``. The
properties asserted here are the ones the two-axis vocabulary is *for*:

* :class:`~mcuhome.workbench.buildtarget.LocalBuild` carries an
  execution and :class:`~mcuhome.workbench.buildtarget.RemoteBuild`
  carries none — a client does not tell somebody else's machine whether
  to start a container, and a field that let it would be the whole
  asymmetry gone;
* a target is **authoritative**: what a caller states on it is what runs,
  whatever the request's target-shaped fields happen to say;
* every target name resolves to the target that describes it, so a
  caller that states a name and a caller that builds the object get the
  same build.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome.workbench import build, buildtarget, containerbuild, sessionclient
from mcuhome.workbench import buildenvsession as lb
from mcuhome.workbench.builders import SelectedBuilder
from mcuhome.workbench.buildlock import holder_of


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


def _build(request: build.BuildRequest, target) -> build.BuildResult:
    """What a caller does at its entry point: one ``asyncio.run``."""
    return asyncio.run(build.build_firmware(request, target=target))


def _local_result(tmp_path, seen: dict):
    """A stand-in for the container composition that records its arguments."""

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        outcome = lb.StepResult(
            action="build",
            context_id="sha256:" + "1" * 64,
            exit_code=0,
            status="success",
            artifacts=(),
            out_dir=tmp_path / "delivery",
        )
        return containerbuild.ContainerBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            container_image="registry.example.test/other/environment:test",
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

    A client may say *where* a build runs and *which* build environment
    it means; it may not tell somebody else's machine **how** to run it —
    that machine's operator configured that, and an ``Execution`` is
    exactly the how. The image pin is the one thing on the other side of
    that line: it names what to build in, which the far side then
    resolves against its own allowlist and may refuse.

    A build server answers a context by constructing a target of its
    own, which is also why the multi-hop case needs no special code: a
    server configured to pass work on constructs a ``RemoteBuild``.
    """
    fields = {field.name for field in dataclasses.fields(buildtarget.RemoteBuild)}
    assert "execution" not in fields
    assert fields == {"server", "token", "wait", "max_wait_seconds", "container_image"}


# --------------------------------------------------------------------------
# A target name is one word for two decisions
# --------------------------------------------------------------------------


def test_the_local_target_is_a_local_build_in_a_container(model, tmp_path) -> None:
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path,
        container_image="registry.example.test/other/environment:test",
    )
    target = build.build_target_for(build.TARGET_LOCAL, request)
    assert target == buildtarget.LocalBuild(
        execution=buildtarget.ContainerExecution(
            container_image="registry.example.test/other/environment:test"
        )
    )


def test_the_remote_target_is_a_remote_build(model, tmp_path) -> None:
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path,
        builder=SelectedBuilder(target=build.TARGET_REMOTE, server="attic:8100", token="a-token"),
        wait_for_turn=False,
        max_wait_seconds=90.0,
    )
    target = build.build_target_for(build.TARGET_REMOTE, request)
    assert target == buildtarget.RemoteBuild(
        server="attic:8100", token="a-token", wait=False, max_wait_seconds=90.0
    )


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_preference_is_the_container_target(model, tmp_path, nothing) -> None:
    """The default survives the translation: no name still means a container."""
    request = build.BuildRequest(model=model, out_dir=tmp_path)
    assert build.build_target_for(nothing, request) == buildtarget.LocalBuild()


def test_an_unknown_target_refuses_at_the_translation(model, tmp_path) -> None:
    """The same refusal as before, one step earlier — not a ``KeyError``."""
    request = build.BuildRequest(model=model, out_dir=tmp_path)
    with pytest.raises(build.UnknownBuildTarget):
        build.build_target_for("cloud", request)


# --------------------------------------------------------------------------
# Reaching a composition through the seam
# --------------------------------------------------------------------------


def test_a_stated_target_beats_the_requests_target_fields(model, tmp_path, monkeypatch) -> None:
    """A caller that builds a target itself is the one that decides.

    ``BuildRequest`` still carries the target-shaped fields for the name
    entry point, and this is what keeps them from becoming a second
    source of truth: the seam reads the target, and the request's copies
    are ignored — which is what makes deleting them later a deletion
    rather than a rewrite.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(build, "compose_local_build", _local_result(tmp_path, seen))
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path,
        container_image="registry.example.test/other/environment:from-the-request",
    )
    outcome = _build(
        request,
        buildtarget.LocalBuild(
            execution=buildtarget.ContainerExecution(
                container_image="registry.example.test/other/environment:from-the-target",
                cache_root=tmp_path / "from-the-target",
            )
        ),
    )
    assert outcome.ok
    assert seen["container_image"] == "registry.example.test/other/environment:from-the-target"
    assert seen["cache_root"] == tmp_path / "from-the-target"


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
            artifacts=(),
            out_dir=tmp_path / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    outcome = _build(
        build.BuildRequest(model=model, out_dir=tmp_path, context_dir=context),
        buildtarget.RemoteBuild(
            server="build.example:8080", token="a-token", wait=False, max_wait_seconds=90.0
        ),
    )
    assert outcome.target == build.TARGET_REMOTE
    assert seen["url"] == "ws://build.example:8080/ws"
    assert seen["token"] == "a-token"
    assert seen["wait"] is False
    assert seen["max_wait"] == 90.0


def test_the_name_entry_point_and_the_seam_run_the_same_build(model, tmp_path, monkeypatch) -> None:
    """A target name and a target object reach the same build.

    ``build_firmware`` takes either, and the translation is the whole of
    the difference: whatever a target name means, it reaches the
    composition with the arguments a caller that built the object itself
    would have passed.
    """
    request = build.BuildRequest(
        model=model,
        out_dir=tmp_path,
        container_image="registry.example.test/other/environment:test",
    )

    by_name: dict[str, object] = {}
    monkeypatch.setattr(build, "compose_local_build", _local_result(tmp_path, by_name))
    assert asyncio.run(build.build_firmware(request, target=build.TARGET_LOCAL)).ok

    by_target: dict[str, object] = {}
    monkeypatch.setattr(build, "compose_local_build", _local_result(tmp_path, by_target))
    assert _build(request, build.build_target_for(build.TARGET_LOCAL, request)).ok

    assert by_name == by_target


# --------------------------------------------------------------------------
# What the seam refuses
# --------------------------------------------------------------------------


def test_a_target_this_package_does_not_run_is_a_type_error(model, tmp_path) -> None:
    """A name can be mistyped; an object cannot.

    So an unimplemented target is a programming mistake and says so,
    rather than borrowing the wording of a refusal a user could act on.
    """
    request = build.BuildRequest(model=model, out_dir=tmp_path)
    with pytest.raises(TypeError, match="BuildTarget"):
        _build(request, buildtarget.BuildTarget())
    with pytest.raises(TypeError, match="Execution"):
        _build(request, buildtarget.LocalBuild(execution=buildtarget.Execution()))


def test_the_seam_holds_the_build_directory(model, tmp_path, monkeypatch) -> None:
    """The guard is at the seam, so every way in inherits it.

    A caller that hands over a target object — a build server, an
    embedder — takes the same lock as one that names a target, because
    the guard sits where the build is dispatched rather than in whatever
    resolved the name.
    """
    seen: dict[str, object] = {}
    inner = _local_result(tmp_path, seen)

    def fake(device_model, **kwargs):
        seen["holder"] = holder_of(tmp_path)
        return inner(device_model, **kwargs)

    monkeypatch.setattr(build, "compose_local_build", fake)
    outcome = _build(
        build.BuildRequest(model=model, out_dir=tmp_path),
        buildtarget.LocalBuild(execution=buildtarget.ContainerExecution()),
    )
    assert outcome.ok
    holder = seen["holder"]
    assert holder is not None
    assert holder["device"] == model.device.name
    assert holder["operation"] == "build"
