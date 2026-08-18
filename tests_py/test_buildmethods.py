# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The three build methods behind one interface (``buildmethods.py``).

No container, no toolchain and no socket: every method is stubbed at its
own backend seam — ``compose_local_build``, ``run_dev_build``,
``run_remote_build`` — and what is asserted is the layer above them. That
is deliberately the whole point of the module: the three compositions are
tested where they live (``test_localbuild.py`` and ``test_sessionclient.py``
here, ``test_workspace.py`` in ``mcuhome-sdk`` since ADR 0024), and this
file asserts that a caller reaches the right one and reads one answer
whichever ran.

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

import pytest
from conftest import EXAMPLES_DIR, resolve_file
from mcuhome.compiler import devbuild
from mcuhome.model.artifacts import Artifact
from mcuhome.model.errors import BuildError
from mcuhome.model.manifest import MANIFEST_FILE

from mcuhome.workbench import buildmethods, containerbuild, sessionclient
from mcuhome.workbench import orchestrator as lb
from mcuhome.workbench.buildlock import holder_of
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE


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
    """Typed, and it names all three — a user who guessed wrong needs them."""
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
# One outcome shape, three methods
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


def test_the_local_dev_method_answers_in_the_same_shape(model, tmp_path, monkeypatch):
    """``local-dev``: a host build, described by its build manifest.

    Two things differ from the container-shaped methods and both are
    facts rather than gaps: there is no build context, so no context ID,
    and no build container declared an artifact list — what a west build
    produced is described by ``build-manifest.json``, which is what
    :attr:`BuildOutcome.report` names.
    """
    seen: dict[str, object] = {}

    def fake(device_model, **kwargs):
        seen.update(kwargs)
        return devbuild.DevBuildResult(
            plan=None, log="", images=[], merged=None, manifest_path=tmp_path / MANIFEST_FILE
        )

    monkeypatch.setattr(devbuild, "run_dev_build", fake)
    outcome = _run(
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            bootloader_key=tmp_path / "signing.pub",
            module_dir=tmp_path / "module",
            started_in=tmp_path,
            snippets=("debug-rtt",),
        ),
        buildmethods.LOCAL_DEV,
    )
    assert outcome.method == buildmethods.LOCAL_DEV
    assert outcome.successful and outcome.status == "success"
    assert outcome.context_id == ""
    assert outcome.artifacts == ()
    assert outcome.out_dir == tmp_path
    assert outcome.report == MANIFEST_FILE
    assert seen["bootloader_key"] == tmp_path / "signing.pub"
    assert seen["snippets"] == ("debug-rtt",)


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
        return devbuild.DevBuildResult(
            plan=None, log="", images=[], merged=None, manifest_path=tmp_path / MANIFEST_FILE
        )

    monkeypatch.setattr(devbuild, "run_dev_build", fake)
    request = buildmethods.BuildRequest(
        model=model,
        out_dir=tmp_path,
        bootloader_key=tmp_path / "signing.pub",
        module_dir=tmp_path / "module",
        started_in=tmp_path,
    )
    assert _run(request, buildmethods.LOCAL_DEV).successful
    holder = seen["holder"]
    assert holder["device"] == model.device.name  # type: ignore[index]
    assert holder["operation"] == "build"  # type: ignore[index]
    # And released again: the next build of that directory just runs.
    assert _run(request, buildmethods.LOCAL_DEV).successful


def test_the_local_dev_method_refuses_without_the_three_things_only_a_caller_knows(model, tmp_path):
    """A public key file and the two workspace hints; a guess is not offered."""
    with pytest.raises(BuildError) as refusal:
        _run(
            buildmethods.BuildRequest(model=model, out_dir=tmp_path),
            buildmethods.LOCAL_DEV,
        )
    assert "public key file" in str(refusal.value)


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
# The optional edge to the compiler: which ImportError means what
# --------------------------------------------------------------------------


def _failing_import(error: ImportError):
    """Replace the one call ``_compiler`` makes, with a stated failure.

    ``importlib.import_module`` goes straight to the import machinery and
    never through ``builtins.__import__``, so this is the seam — the same
    place the ``aiohttp`` test above stubs, one module along.
    """
    import importlib

    def refuse(name: str):
        raise error

    return importlib, refuse


def test_a_missing_compiler_distribution_is_the_named_refusal(model, tmp_path, monkeypatch):
    """The compiler is optional (ADR 0020 decision 3), so its absence is a sentence.

    ``local-dev`` is the one method that still asks for it: it compiles in
    the caller's own west workspace, which is what ``mcuhome.compiler``
    knows how to drive. The container method used to ask too and does not
    any more — the test below is that half.
    """
    importlib, refuse = _failing_import(
        ModuleNotFoundError("No module named 'mcuhome.compiler'", name="mcuhome.compiler")
    )
    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(buildmethods.MethodUnavailable) as refusal:
        _run(buildmethods.BuildRequest(model=model, out_dir=tmp_path), buildmethods.LOCAL_DEV)
    assert "pip install mcuhome-compiler" in str(refusal.value.hint)


def test_a_broken_dependency_inside_an_installed_compiler_surfaces_unchanged(
    model, tmp_path, monkeypatch
):
    """An ImportError from *within* the compiler is not evidence of its absence.

    A wheel built for another interpreter fails the same ``import_module``
    call, and translating that into "mcuhome-compiler is not installed"
    would send the reader to reinstall the one piece that is already
    there. The discriminator is the failing import's own name.
    """
    broken = ImportError("libzstd.so.1: cannot open shared object file", name="zstandard")
    importlib, refuse = _failing_import(broken)
    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(ImportError) as raised:
        _run(buildmethods.BuildRequest(model=model, out_dir=tmp_path), buildmethods.LOCAL_DEV)
    assert raised.value is broken
    assert not isinstance(raised.value, buildmethods.MethodUnavailable)


def test_the_container_method_no_longer_asks_for_the_compiler(model, tmp_path, monkeypatch):
    """The orchestrator is the workbench's own, so ``local`` needs no compiler.

    This is the point of moving it. A container build needs a container
    runtime and nothing else of a toolchain — that was always the claim,
    and until the orchestrator lived here it was untrue at the level of
    installed distributions: ``local`` refused without ``mcuhome-compiler``
    even though nothing it ran came from the container's own package.

    Asserted by making *every* dynamic import fail and showing that the
    build gets past it — it stops at the first thing it genuinely needs,
    an SDK source, and names that instead.
    """
    importlib, refuse = _failing_import(
        ModuleNotFoundError("No module named 'mcuhome.compiler'", name="mcuhome.compiler")
    )
    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(BuildError) as refusal:
        _run(buildmethods.BuildRequest(model=model, out_dir=tmp_path), buildmethods.LOCAL)
    assert not isinstance(refusal.value, buildmethods.MethodUnavailable)
    assert "SDK source" in str(refusal.value)


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
