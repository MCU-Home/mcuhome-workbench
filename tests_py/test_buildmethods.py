# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The three build methods behind one interface (``buildmethods.py``).

No container, no toolchain and no socket: every method is stubbed at its
own backend seam — ``run_local_build``, ``run_dev_build``,
``run_remote_build`` — and what is asserted is the layer above them. That
is deliberately the whole point of the module: the three compositions are
tested where they live (``test_localbuild.py``, ``test_workspace.py``,
``test_sessionclient.py``), and this file asserts that a caller reaches
the right one and reads one answer whichever ran.

The properties, in the order they matter:

* the outcome shape does not depend on the method (E56) — success, the
  delivery directory, and the *name of the build report*, which is what
  the one shared host-side signing step needs;
* a method name nobody implements is a refusal that lists the ones that
  exist, rather than a ``KeyError`` or a silent default;
* ``remote`` refuses in words for the two things it cannot invent — the
  build server's address (E53) and a build context — and for the missing
  transport extra, before any of them costs a connection.
"""

from __future__ import annotations

import asyncio
import builtins

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome.compiler import devbuild, localbuild
from mcuhome.compiler import localbackend as lb
from mcuhome.model.artifacts import Artifact
from mcuhome.model.manifest import MANIFEST_FILE
from mcuhome.workbench import buildmethods, sessionclient
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
        return localbuild.LocalBuildResult(
            outcome=outcome,
            out_dir=tmp_path / "delivery",
            context_dir=tmp_path / "context",
            image="ghcr.io/mcu-home/builder:test",
        )

    monkeypatch.setattr(localbuild, "run_local_build", fake)
    outcome = _run(
        buildmethods.BuildRequest(
            model=model,
            out_dir=tmp_path,
            signing_pub="-----BEGIN PUBLIC KEY-----\n",
            sdk_sources=(tmp_path / "sdk",),
            image="ghcr.io/mcu-home/builder:test",
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
    assert outcome.image == "ghcr.io/mcu-home/builder:test"
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


def test_the_local_dev_method_refuses_without_the_three_things_only_a_caller_knows(model, tmp_path):
    """A public key file and the two workspace hints; a guess is not offered."""
    from mcuhome.model.errors import BuildError

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


def test_remote_without_a_server_refuses_naming_both_decided_rungs(model, tmp_path) -> None:
    """E53's ladder, as a refusal: the flag and the variable, by name.

    There is no default build server and no discovery — the context
    carries the device model, so where it is sent is a decision. The
    refusal therefore names the two rungs the decision log settled and
    stops; the third (a file under ``$XDG_CONFIG_HOME/mcuhome/``) has no
    decided format and is not invented here.
    """
    with pytest.raises(buildmethods.RemoteNotConfigured) as refusal:
        _run(
            buildmethods.BuildRequest(model=model, out_dir=tmp_path),
            buildmethods.REMOTE,
        )
    rendered = str(refusal.value)
    assert "--server" in rendered
    assert "MCUHOME_BUILD_SERVER" in rendered
    assert "MCUHOME_BUILD_TOKEN" in rendered


def test_remote_without_a_context_says_which_step_is_missing(model, tmp_path) -> None:
    """The honest gap: nothing resolves the container digest for a remote build.

    The context pins the build container by digest, and for ``remote``
    that digest is meant to come from the server's own ``capabilities``
    answer (E53). Until that exists, a caller holding a locked context
    passes it and a caller holding only a model is told so — rather than
    getting a context pinned to something this machine happened to have.
    """
    with pytest.raises(buildmethods.RemoteNotConfigured) as refusal:
        _run(
            buildmethods.BuildRequest(
                model=model, out_dir=tmp_path, server="ws://build.example/session"
            ),
            buildmethods.REMOTE,
        )
    rendered = str(refusal.value)
    assert "build context" in rendered
    assert "capabilities" in rendered


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
    """The compiler is optional (ADR 0020 decision 3), so its absence is a sentence."""
    importlib, refuse = _failing_import(
        ModuleNotFoundError("No module named 'mcuhome.compiler'", name="mcuhome.compiler")
    )
    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(buildmethods.MethodUnavailable) as refusal:
        _run(buildmethods.BuildRequest(model=model, out_dir=tmp_path), buildmethods.LOCAL)
    assert "pip install mcuhome-compiler" in str(refusal.value.hint)


def test_a_broken_dependency_inside_an_installed_compiler_surfaces_unchanged(
    model, tmp_path, monkeypatch
):
    """An ImportError from *within* the compiler is not evidence of its absence.

    A ``zstandard`` wheel built for another interpreter fails the same
    ``import_module`` call, and translating that into "mcuhome-compiler is
    not installed" would send the reader to reinstall the one piece that
    is already there. The discriminator is the failing import's own name.
    """
    broken = ImportError("libzstd.so.1: cannot open shared object file", name="zstandard")
    importlib, refuse = _failing_import(broken)
    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(ImportError) as raised:
        _run(buildmethods.BuildRequest(model=model, out_dir=tmp_path), buildmethods.LOCAL)
    assert raised.value is broken
    assert not isinstance(raised.value, buildmethods.MethodUnavailable)


# --------------------------------------------------------------------------
# The dependency arrow, at run time
# --------------------------------------------------------------------------


def test_importing_the_dispatch_does_not_drag_in_the_compiler() -> None:
    """ADR 0020 decision 3, as the property rather than as syntax.

    ``test_packaging.py`` asserts no ``import mcuhome.compiler`` appears
    in this package's syntax tree; that is the rule, and this is what the
    rule is *for*: a dashboard install that carries no toolchain must be
    able to import the dispatch. Checked in a fresh interpreter, because
    this suite's own imports have long since loaded everything.
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
