# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build methods behind one interface (``buildmethods.py``).

No container and no socket: each method is stubbed at its own backend
seam — ``compose_local_build``, ``run_remote_build`` — and what is
asserted is the layer above them. That is deliberately the whole point of
the module: the compositions are tested where they live
(``test_localbuild.py``, ``test_sessionclient.py``), and this file asserts
that a caller reaches the right one and reads one answer whichever ran.

The properties, in the order they matter:

* the outcome shape does not depend on the method (E56) — success, the
  delivery directory, and the *name of the build report*, which is what
  the one shared host-side signing step needs;
* a method name nobody implements is a refusal that lists the ones that
  exist, rather than a ``KeyError`` or a silent default;
* ``remote`` refuses in words for the two things it cannot invent — the
  build server's address (E53) and the SDK source its context is pinned
  from (E65) — and for the missing transport extra, before any of them
  costs a connection.

What ``remote`` *does* once it has both — resolve the pin, write the base
context, drive a real session — is asserted in ``test_sessionclient.py``,
against the real build server. Here the composition is stubbed at
``run_remote_build`` and only the layer above it is the subject.
"""

from __future__ import annotations

import asyncio
import builtins
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, make_package_source, resolve_file
from mcuhome.model.artifacts import Artifact
from mcuhome.model.errors import BuildError

from mcuhome.workbench import buildmethods, containerbuild, sessionclient, subprocessbuild
from mcuhome.workbench import orchestrator as lb
from mcuhome.workbench.buildlock import holder_of
from mcuhome.workbench.contextdir import create_build_context
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE
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


def _run(request: buildmethods.BuildRequest, method: str) -> buildmethods.BuildOutcome:
    """What a command line does at its entry point: one ``asyncio.run``."""
    return asyncio.run(buildmethods.run_build(request, method=method))


# --------------------------------------------------------------------------
# Choosing a method
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", buildmethods.METHODS)
def test_every_method_name_resolves_to_itself(name: str) -> None:
    assert buildmethods.resolve_method(name) == name


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_preference_is_the_local_container(nothing) -> None:
    """E54: the default is the build container on this machine."""
    assert buildmethods.resolve_method(nothing) == buildmethods.LOCAL
    assert buildmethods.DEFAULT_METHOD == buildmethods.LOCAL


def test_an_unknown_method_is_a_refusal_that_lists_the_real_ones() -> None:
    """Typed, and it names them all — a user who guessed wrong needs them."""
    with pytest.raises(buildmethods.UnknownMethod) as refusal:
        buildmethods.resolve_method("cloud")
    rendered = str(refusal.value)
    assert '"cloud"' in rendered
    for name in buildmethods.METHODS:
        assert name in rendered


def test_run_build_refuses_an_unknown_method_before_it_runs_anything(model, tmp_path) -> None:
    request = buildmethods.BuildRequest(model=model, out_dir=tmp_path)
    with pytest.raises(buildmethods.UnknownMethod):
        _run(request, "cloud")


# --------------------------------------------------------------------------
# Choosing how this machine executes it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", buildmethods.BUILD_MODES)
def test_every_build_mode_resolves_to_itself(name: str) -> None:
    assert buildmethods.resolve_build_mode(name) == name


@pytest.mark.parametrize("nothing", [None, ""])
def test_no_preference_is_the_container(nothing) -> None:
    """The default is the container: it is the only mode that isolates.

    A request that states no mode states none — the configuration
    answers, and with nothing configured that answer is the container.
    So the default is asserted where it is decided, on the target, and
    not on a field that now means "nobody said".
    """
    assert buildmethods.resolve_build_mode(nothing) == buildmethods.MODE_CONTAINER
    assert buildmethods.DEFAULT_BUILD_MODE == buildmethods.MODE_CONTAINER
    assert buildmethods.BuildRequest(model=None, out_dir=Path()).build_mode is None
    assert buildmethods.BuildOptions().mode == buildmethods.MODE_CONTAINER


def test_an_unknown_build_mode_is_a_refusal_that_lists_the_real_ones() -> None:
    with pytest.raises(buildmethods.UnknownBuildMode) as refusal:
        buildmethods.resolve_build_mode("vm")
    rendered = str(refusal.value)
    assert '"vm"' in rendered
    for name in buildmethods.BUILD_MODES:
        assert name in rendered


def test_the_mode_selects_the_execution_the_local_method_runs(model, tmp_path) -> None:
    """One name, two decisions: the target states them apart."""
    container = buildmethods.target_for_method(
        buildmethods.LOCAL, buildmethods.BuildRequest(model=model, out_dir=tmp_path)
    )
    assert isinstance(container.execution, buildmethods.ContainerExecution)

    subprocess_target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            build_mode=buildmethods.MODE_SUBPROCESS,
            ccache_dir=tmp_path / "ccache",
        ),
    )
    execution = subprocess_target.execution
    assert isinstance(execution, buildmethods.SubprocessExecution)
    assert execution.ccache_dir == tmp_path / "ccache"


def test_the_subprocess_mode_reaches_its_own_composition(model, tmp_path, monkeypatch):
    """``build.mode = subprocess`` selects the backend, and nothing else does."""
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        outcome = lb.LocalOutcome(
            action="build",
            context_id="sha256:" + "2" * 64,
            exit_code=0,
            status="success",
            successful=True,
            artifacts=_artifacts(),
            out=tmp_path / "delivery",
        )
        return subprocessbuild.SubprocessBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            environment=None,
        )

    monkeypatch.setattr(buildmethods, "compose_subprocess_build", fake)
    outcome = _run(
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            build_mode=buildmethods.MODE_SUBPROCESS,
            ccache_dir=tmp_path / "ccache",
            jobs=2,
        ),
        buildmethods.LOCAL,
    )
    assert outcome.method == buildmethods.LOCAL
    assert outcome.successful
    assert outcome.artifacts == _artifacts()
    assert outcome.report == BUILD_REPORT_FILE
    # No image ran, and the outcome says so rather than naming one.
    assert outcome.image == ""
    assert seen["ccache_dir"] == tmp_path / "ccache"
    assert seen["jobs"] == 2


def test_a_subprocess_build_resolves_its_pins_like_every_other_build(model, tmp_path) -> None:
    """The mode is no longer blocked, and the first thing it needs is a pin.

    A subprocess build creates its own context now — that is what makes
    it a build method rather than half of one — so a request with no
    package source at all fails where every other build method fails: at
    the SDK pin, naming the setting that supplies one. The old refusal
    ("nothing states which packages to use") is gone, and this test is
    what would notice it coming back.
    """
    with pytest.raises(BuildError) as refusal:
        asyncio.run(
            buildmethods.build_firmware(
                buildmethods.BuildRequest(
                    model=model,
                    out_dir=tmp_path,
                    signing_pub=_PUBLIC_PEM,
                    build_mode=buildmethods.MODE_SUBPROCESS,
                ),
                target=buildmethods.LocalBuild(execution=buildmethods.SubprocessExecution()),
            )
        )
    assert "sdk_sources" in refusal.value.hint
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
            outcome=lb.LocalOutcome(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=context_dir,
            environment=kwargs["environment"],
        )

    monkeypatch.setattr(subprocessbuild, "run_locked_build", fake_run)
    monkeypatch.setattr(buildmethods, "lock_context", lambda directory: None)
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
        signing_pub=_PUBLIC_PEM,
    )
    steps: list[tuple] = []
    result = buildmethods.compose_subprocess_build(
        model,
        sdk_sources=(tmp_path / "sdk",),
        work_root=tmp_path / "work",
        env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        environment=FakeEnvironment(),
        context_dir=tmp_path / "context",
        jobs=5,
        on_step=lambda name, **facts: steps.append((name, facts)),
    )
    assert result.out_dir == tmp_path / "out"
    assert driven["jobs"] == 5
    # The context's own pins are what the environment is checked against,
    # and the device's Zephyr constraint travels with them.
    assert checked["pin"].workspace.name == "mcuhome-build-workspace"
    assert checked["zephyr_constraint"] == model.toolchain.zephyr_constraint
    assert checked["generator"].startswith("mcuhome-workbench:")
    # Nobody configured a cache, so it is the user's cache directory —
    # the same answer a container build gets, from the same resolution.
    assert driven["ccache_dir"] == tmp_path / "cache" / "mcuhome" / "ccache"
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
    monkeypatch.setattr(buildmethods, "lock_context", lambda directory: order.append("lock"))
    monkeypatch.setattr(
        subprocessbuild,
        "run_locked_build",
        lambda context_dir, **kwargs: subprocessbuild.SubprocessBuildResult(
            outcome=lb.LocalOutcome(action="build", context_id="", exit_code=0),
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
        signing_pub=_PUBLIC_PEM,
    )
    buildmethods.compose_subprocess_build(
        model,
        sdk_sources=(tmp_path / "sdk",),
        work_root=tmp_path / "work",
        env={},
        environment=FakeEnvironment(),
        context_dir=tmp_path / "context",
    )
    assert order == ["check", "lock"]


# --------------------------------------------------------------------------
# One outcome shape, both methods
# --------------------------------------------------------------------------


def test_the_local_method_answers_with_the_backends_own_verdict(model, tmp_path, monkeypatch):
    """``local``: the §5.3 outcome, unchanged, in the shared shape."""
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        outcome = lb.LocalOutcome(
            action="build",
            context_id="sha256:" + "1" * 64,
            exit_code=0,
            status="success",
            successful=True,
            artifacts=_artifacts(),
            out=tmp_path / "delivery",
        )
        return containerbuild.LocalBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            image="ghcr.io/mcu-home/build-container:test",
        )

    monkeypatch.setattr(buildmethods, "compose_local_build", fake)
    outcome = _run(
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            signing_pub="-----BEGIN PUBLIC KEY-----\n",
            sdk_sources=(tmp_path / "sdk",),
            image="ghcr.io/mcu-home/build-container:test",
            jobs=3,
        ),
        buildmethods.LOCAL,
    )
    assert outcome.method == buildmethods.LOCAL
    assert outcome.successful and outcome.status == "success"
    assert outcome.context_id == "sha256:" + "1" * 64
    assert outcome.artifacts == _artifacts()
    assert outcome.out_dir == tmp_path / "delivery"
    assert outcome.report == BUILD_REPORT_FILE
    assert outcome.image == "ghcr.io/mcu-home/build-container:test"
    # The scratch area defaults under the build directory, and the public
    # key travelled — no private key is a field of the request at all.
    assert seen["work_root"] == tmp_path / ".mcuhome-local"
    assert seen["jobs"] == 3
    assert seen["signing_pub"] == "-----BEGIN PUBLIC KEY-----\n"


def test_a_build_holds_its_build_directory_while_it_runs(model, tmp_path, monkeypatch):
    """Whichever method runs, the directory is taken for its duration.

    The guard sits at the dispatch and not in a method because every
    method writes into the same directory — and the two runs that
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
        outcome = lb.LocalOutcome(
            action="build",
            context_id="sha256:" + "1" * 64,
            exit_code=0,
            status="success",
            successful=True,
            artifacts=_artifacts(),
            out=tmp_path / "delivery",
        )
        return containerbuild.LocalBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            image="ghcr.io/mcu-home/build-container:test",
        )

    monkeypatch.setattr(buildmethods, "compose_local_build", fake)
    request = buildmethods.BuildRequest(model=model, out_dir=tmp_path)
    assert _run(request, buildmethods.LOCAL).successful
    holder = seen["holder"]
    assert holder["device"] == model.device.name  # type: ignore[index]
    assert holder["operation"] == "build"  # type: ignore[index]
    # And released again: the next build of that directory just runs.
    assert _run(request, buildmethods.LOCAL).successful


def test_the_remote_method_answers_in_the_same_shape(model, tmp_path, monkeypatch):
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
            successful=True,
            artifacts=_artifacts(),
            out=tmp_path / "out",
            invocation_id="inv-1",
        )

    monkeypatch.setattr(sessionclient, "run_remote_build", fake)
    outcome = _run(
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            server="ws://build.example:8080/session",
            token="a-token",
            context_dir=context,
        ),
        buildmethods.REMOTE,
    )
    assert outcome.method == buildmethods.REMOTE
    assert outcome.successful and outcome.status == "success"
    assert outcome.context_id == "sha256:" + "2" * 64
    assert outcome.artifacts == _artifacts()
    assert outcome.out_dir == tmp_path / "out"
    # The same report name as the local method: both are deliveries out of
    # a build container, so one host-side signer reads either (E55, E56).
    assert outcome.report == BUILD_REPORT_FILE
    assert seen["context_dir"] == context
    assert seen["url"] == "ws://build.example:8080/session"
    assert seen["token"] == "a-token"
    assert seen["work_root"] == tmp_path / ".mcuhome-remote"


# --------------------------------------------------------------------------
# What `remote` cannot invent
# --------------------------------------------------------------------------


def test_remote_without_a_server_refuses_naming_both_rungs(model, tmp_path) -> None:
    """ADR 0023's ladder, as a refusal: a configured builder, or fully manual.

    There is no default build server and no discovery — the context
    carries the device model, so where it is sent is a decision. The
    refusal therefore names both ways of making it: a configured remote
    builder (with its token's place in the secrets layout) and the
    fully manual ``--build-mode`` rung. Builder selection is read by
    the caller rather than by this package, and is named here anyway:
    this is the text a user reads when nothing is configured, and a
    rung it does not mention is a rung nobody finds.
    """
    with pytest.raises(buildmethods.RemoteNotConfigured) as refusal:
        _run(
            buildmethods.BuildRequest(model=model, out_dir=tmp_path),
            buildmethods.REMOTE,
        )
    rendered = str(refusal.value)
    assert "type: remote" in rendered
    assert "--builder attic" in rendered
    assert "default_builder" in rendered
    assert "secrets/build-server/attic.yaml" in rendered
    assert "--build-mode remote --build-server" in rendered
    assert "--build-token" in rendered


def test_remote_without_an_sdk_source_names_the_two_knobs(model, tmp_path) -> None:
    """E65's other half: the pin is the client's, so its source must be too.

    ``remote`` creates its own context now, and a context is
    content-addressed over the SDK package's hash — so the one thing this
    method still cannot invent is *which package*. The refusal names the
    same two knobs the ``local`` method reads, because they are the same
    two knobs: the pin is resolved here either way, and only who fetches
    the bytes afterwards differs. It deliberately does not fall back to
    "whatever the server has", which would be an identity describing a
    build nobody asked for.
    """
    with pytest.raises(buildmethods.RemoteNotConfigured) as refusal:
        _run(
            buildmethods.BuildRequest(
                model=model, out_dir=tmp_path, server="ws://build.example/session"
            ),
            buildmethods.REMOTE,
        )
    rendered = str(refusal.value)
    assert "--sdk-sources" in rendered
    assert "sdk_sources" in rendered


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
            buildmethods.BuildRequest(
                model=model,
                out_dir=tmp_path,
                server="ws://build.example/session",
                context_dir=context,
            ),
            buildmethods.REMOTE,
        )
    assert "pip install 'mcuhome-workbench[remote]'" in str(refusal.value)


# --------------------------------------------------------------------------
# No build reaches for the compiler at all
# --------------------------------------------------------------------------


def test_the_container_method_no_longer_asks_for_the_compiler(model, tmp_path, monkeypatch):
    """The orchestrator is the workbench's own, so no build needs a compiler.

    This is the point of moving it. A build needs a container runtime and
    nothing else of a toolchain — that was always the claim, and until the
    orchestrator lived here it was untrue at the level of installed
    distributions: ``local`` refused without ``mcuhome-compiler`` even
    though nothing it ran came from the container's own package.

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
    monkeypatch.setattr(lb, "_run_command", lambda argv, on_line=None: lb.Completed(1, ""))
    with pytest.raises(BuildError) as refusal:
        _run(buildmethods.BuildRequest(model=model, out_dir=tmp_path), buildmethods.LOCAL)
    assert "mcuhome-compiler" not in str(refusal.value)


# --------------------------------------------------------------------------
# The dependency arrow, at run time
# --------------------------------------------------------------------------


def test_importing_the_dispatch_does_not_drag_in_the_compiler() -> None:
    """ADR 0020 decision 3, as the property rather than as syntax.

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
        "import sys; import mcuhome.workbench.buildmethods; "
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
        "edge is optional (ADR 0020 decision 3) and must stay resolved at "
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
    assert buildmethods.websocket_url(address) == url


@pytest.mark.parametrize("address", ["", "   ", "://attic", "ftp://attic", "ssh://attic:22"])
def test_an_address_that_is_not_one_is_refused_in_words(address) -> None:
    """A refusal naming what is wrong, never a traceback from the socket."""
    with pytest.raises(buildmethods.RemoteNotConfigured) as refusal:
        buildmethods.websocket_url(address)
    assert refusal.value.hint
    assert "<host" in refusal.value.hint


def test_the_developer_trees_reach_the_execution(model, tmp_path) -> None:
    """``build.dev_workspace`` and ``build.dev_tools`` are read where every
    other method-specific field is read, and land on the target."""
    target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            build_mode=buildmethods.MODE_SUBPROCESS,
            dev_workspace=tmp_path / "workspace-tree",
            dev_tools=tmp_path / "tools-tree",
        ),
    )
    assert target.execution.dev_workspace == tmp_path / "workspace-tree"
    assert target.execution.dev_tools == tmp_path / "tools-tree"


def test_half_a_developer_environment_is_refused(model, tmp_path) -> None:
    """An environment is a set of packages, and half a set is not one: a
    workspace of one version against tools of another fails deep inside a
    compile with nothing to point at."""
    for stated in ("dev_workspace", "dev_tools"):
        with pytest.raises(lb.EnvironmentUnavailable, match="both halves") as refusal:
            asyncio.run(
                buildmethods.build_firmware(
                    buildmethods.BuildRequest(model=model, out_dir=tmp_path),
                    target=buildmethods.LocalBuild(
                        execution=buildmethods.SubprocessExecution(**{stated: tmp_path / "tree"})
                    ),
                )
            )
        assert "build.dev_workspace" in refusal.value.hint
        assert "build.dev_tools" in refusal.value.hint


def test_a_developer_environment_reaches_the_composition(model, tmp_path, monkeypatch) -> None:
    """The trees a developer stated become the environment the subprocess
    composition runs against, in place of the store's entries."""
    workspace = tmp_path / "workspace-tree"
    (workspace / "workspace").mkdir(parents=True)
    (workspace / "build-workspace.json").write_text(
        '{"package": "mcuhome-build-workspace", "version": "0.1.0+dev", "workspace": "workspace"}',
        encoding="utf-8",
    )
    tools = tmp_path / "tools-tree"
    (tools / "bin").mkdir(parents=True)
    entry = tools / "bin" / "build-environment-entry"
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entry.chmod(0o755)
    (tools / "build-tools.json").write_text(
        '{"package": "mcuhome-build-tools_linux-amd64", "version": "0.1.0+dev"}',
        encoding="utf-8",
    )

    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        return subprocessbuild.SubprocessBuildResult(
            outcome=lb.LocalOutcome(action="build", context_id="", exit_code=0),
            out_dir=tmp_path / "out",
            context_dir=tmp_path / "context",
            environment=kwargs["environment"],
        )

    monkeypatch.setattr(buildmethods, "compose_subprocess_build", fake)
    asyncio.run(
        buildmethods.build_firmware(
            buildmethods.BuildRequest(model=model, out_dir=tmp_path),
            target=buildmethods.LocalBuild(
                execution=buildmethods.SubprocessExecution(dev_workspace=workspace, dev_tools=tools)
            ),
        )
    )
    environment = seen["environment"]
    assert environment.developer is True
    assert environment.workspace.path == workspace
    assert environment.tools.path == tools


def test_a_patched_context_is_refused_before_the_context_is_locked(
    model, tmp_path, monkeypatch
) -> None:
    """The refusal comes before anything the build would have changed.

    Locking writes into a directory the user keeps, so a dev-mode build
    that is going to be refused for its patches must not have written the
    lock into that directory first — nor fetched an SDK, nor talked to a
    registry.
    """
    workspace = tmp_path / "workspace-tree"
    (workspace / "workspace").mkdir(parents=True)
    (workspace / "build-workspace.json").write_text(
        '{"package": "mcuhome-build-workspace", "version": "0.1.0+dev", "workspace": "workspace"}',
        encoding="utf-8",
    )
    tools = tmp_path / "tools-tree"
    (tools / "bin").mkdir(parents=True)
    entry = tools / "bin" / "build-environment-entry"
    entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entry.chmod(0o755)
    (tools / "build-tools.json").write_text(
        '{"package": "mcuhome-build-tools_linux-amd64", "version": "0.1.0+dev"}',
        encoding="utf-8",
    )
    context = tmp_path / "context"
    (context / "patches" / "zephyr").mkdir(parents=True)
    (context / "patches" / "zephyr" / "0001-fix.patch").write_text("--- a\n+++ b\n")

    locked: list[Path] = []
    monkeypatch.setattr(buildmethods, "lock_context", lambda directory: locked.append(directory))
    monkeypatch.setattr(
        subprocessbuild,
        "run_locked_build",
        lambda *arguments, **keywords: pytest.fail("the backend must not be reached"),
    )

    with pytest.raises(subprocessbuild.BuildEnvironmentError, match="cannot apply them"):
        asyncio.run(
            buildmethods.build_firmware(
                buildmethods.BuildRequest(model=model, out_dir=tmp_path, context_dir=context),
                target=buildmethods.LocalBuild(
                    execution=buildmethods.SubprocessExecution(
                        dev_workspace=workspace, dev_tools=tools
                    )
                ),
            )
        )
    assert locked == []
    assert not (context / "manifest.yaml").exists()
    assert not (tmp_path / ".mcuhome-local").exists()
