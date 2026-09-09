# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers for the workbench tests.

Everything here is the tools repository's half of what used to be one
suite. :mod:`mcuhome.model` and :mod:`mcuhome.compiler` moved to
``mcuhome-sdk`` with the repository split and are *installed
dependencies* now, not sources in this tree — so they are imported
freely and never searched.
Every whole-package invariant runs over :data:`PACKAGES`, which names
:mod:`mcuhome.workbench` and nothing else.

The device configurations the resolver is exercised against live in
``data/examples/``. They used to be read out of ``docs/design/examples/``,
which went to the SDK repository with the rest of ``docs/design/``; they
are test input here — the golden model in ``data/golden/`` is pinned
against what this repository's resolver makes of them — so the input
travels with the suite rather than with a documentation directory this
repository no longer owns.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from mcuhome.model import buildenvironment
from mcuhome.model.errors import ConfigError, ConfigErrorGroup
from mcuhome.model.model import DeviceModel

from mcuhome.workbench import configuration, containerbuild, ociregistry
from mcuhome.workbench.api import load_model
from mcuhome.workbench.project import Project, find_project_root

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
DATA_DIR = TESTS_DIR / "data"
EXAMPLES_DIR = DATA_DIR / "examples"
GOLDEN_DIR = DATA_DIR / "golden"

#: The import package the three distributions share, and the
#: directory it is assembled from in this checkout. It is a PEP 420
#: namespace package, which is why the directory is named here at all:
#: the import system cannot enumerate one. ``find_spec("mcuhome")``
#: answers with ``origin is None`` and a search-location list that holds
#: path-hook tokens rather than directories under an editable install, so
#: "what is under the namespace" is a question about the tree.
NAMESPACE = "mcuhome"
NAMESPACE_DIR = REPO_ROOT / NAMESPACE

#: The packages the whole-package invariant searches must cover — the one
#: distribution this repository ships, by import
#: name. ``mcuhome.model`` and ``mcuhome.compiler`` are the SDK
#: repository's since the repository split and are covered by the same
#: searches there; a copy of either appearing in this tree would be the defect,
#: which is what the enumeration in :func:`package_modules` catches.
PACKAGES = ("mcuhome.workbench",)


def package_modules() -> list[Path]:
    """Every ``.py`` file of every package the invariants have to cover.

    Derived from the importable packages rather than from one module's
    directory. A directory glob reads "every module there is" only while
    there is one package; after the split into three it would keep passing
    while quietly examining fewer files, which is worse than not
    searching at all. Callers assert that a module they know must be
    examined came back, so the day this list falls behind is the day a
    test fails.

    Three things are checked here rather than left to those callers,
    because none of them has a module a caller could name:

    * ``mcuhome`` is still a namespace package. An ``__init__.py`` there
      would have to belong to one of the distributions that all deliver
      into that directory, and PEP 420 forbids it for exactly that reason.
    * No module sits directly under the namespace directory. Such a file
      is in no distribution, ships with none of them, and is invisible to
      every search below.
    * :data:`PACKAGES` lists every subpackage there is, and each one is
      imported *from this checkout*. The second half matters as much as
      the first: against a non-editable install the searches would read
      copies in ``site-packages`` while the tests exercise the tree. Since
      the repository split it does a third job — a leftover ``mcuhome/model/`` or
      ``mcuhome/compiler/`` directory here is not inert: it is an earlier
      portion of the same namespace and shadows the SDK's real package,
      so the enumeration failing is the only warning anybody gets.
    """
    spec = importlib.util.find_spec(NAMESPACE)
    assert spec is not None, f"{NAMESPACE} is not importable"
    assert spec.origin is None, (
        f"{NAMESPACE} has become a regular package (origin={spec.origin}). "
        "PEP 420 forbids an __init__.py there — several distributions deliver "
        "into that directory and only one of them could own the file."
    )

    loose = sorted(path.name for path in NAMESPACE_DIR.glob("*.py"))
    assert not loose, (
        f"{loose} sit directly under {NAMESPACE_DIR} — no distribution "
        "ships them and no invariant searches them"
    )
    # Every directory, not only those carrying an __init__.py: a
    # directory without one is a namespace *portion*, which is exactly
    # how a stale mcuhome/model/ shadows the installed mcuhome.model
    # rather than being ignored.
    subpackages = {path.name for path in NAMESPACE_DIR.iterdir() if path.is_dir()} - {"__pycache__"}
    expected = {name.rpartition(".")[2] for name in PACKAGES}
    assert subpackages == expected, (
        f"the namespace holds {sorted(subpackages)} but the searches cover "
        f"{sorted(expected)} — extend conftest.PACKAGES"
    )

    found: list[Path] = []
    for name in PACKAGES:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin is not None, f"{name} is not importable"
        directory = Path(spec.origin).parent
        assert directory == NAMESPACE_DIR / name.rpartition(".")[2], (
            f"{name} imports from {directory}, not from this checkout — the "
            "invariants would search files the tests do not run"
        )
        found.extend(directory.glob("*.py"))
    assert found, "the invariant searches would examine nothing"
    return sorted(found)


@pytest.fixture(autouse=True)
def _no_real_signing_key(monkeypatch, tmp_path):
    """No test may touch the developer's own firmware signing key.

    The key lives per project (``secrets/firmware/
    mcuboot.yaml``), but ``MCUHOME_SIGNING_KEY``
    still names a real, long-lived private key file wherever the
    developer set it. A test that reaches one would either read a
    secret it has no business reading or — worse — create one silently
    outside a temporary directory. Point the variables at the test's
    own tmp_path instead; tests that care about the resolution rules
    pass an explicit ``env``.

    ``HOME`` is redirected as well, and not for symmetry: without
    ``XDG_CONFIG_HOME`` the key sits under ``~/.config``, so the two
    variables are two names for the same directory and covering one of
    them covers half the paths that lead there.

    **What this fixture no longer has to catch.** The package itself
    stopped reading the process — ``tests/python/test_userpaths_workbench.py``
    proves it for every module — so nothing here resolves a key out of the
    environment pytest happens to run in. What is left for this fixture
    is everything that hands the process environment *in*: the command
    line's ``env=os.environ``, and any test that does the same.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("MCUHOME_SIGNING_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_system_layer(monkeypatch, tmp_path_factory):
    """No test may read the machine's own ``/etc/mcuhome``.

    The system layer is the one configuration layer that is not derived
    from a stated environment by convention — a machine's is where the
    machine says it is — so a developer or a CI image that has
    ``/etc/mcuhome/configuration.yaml`` would feed it into every test
    that resolves settings, and the suite would answer differently there
    than here. ``XDG_CONFIG_DIRS`` is what states the directory
    (:func:`mcuhome.workbench.configuration.system_config_dir`), so the
    process gets one pointing at this test's own empty directory, and an
    environment a test *states* without that variable is answered with
    the same empty directory rather than with the real one.

    A test about the resolution itself states ``XDG_CONFIG_DIRS`` and is
    answered by the real function; one that patches
    ``configuration.system_config_dir`` outright wins over this fixture,
    because its monkeypatch is applied later.
    """
    # Deliberately not under the test's own tmp_path: several tests
    # require that directory to be empty, and a fixture that put
    # something in it would break them for a reason nobody would look
    # for here.
    empty = tmp_path_factory.mktemp("system-config")
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(empty))
    real = configuration.system_config_dir

    def stated_or_empty(env):
        return real(env) if env.get("XDG_CONFIG_DIRS") else empty / "mcuhome"

    monkeypatch.setattr(configuration, "system_config_dir", stated_or_empty)


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    """Nothing in this suite is allowed to reach a container runtime.

    A safety net, not a convenience: `mcuhome build` now defaults to the
    container, so a test that forgets to stub the runtime would otherwise
    quietly start a real Matter build on the machine running pytest —
    minutes of CPU and gigabytes of build directory, from a suite whose
    whole promise is one second. Tests that want a working preflight
    replace this with their own runner, which wins because their
    monkeypatch is applied later.
    """

    def refuse_argv(argv, on_line=None):
        del on_line
        raise AssertionError(
            f"a test tried to run {argv[0]!r}: the container runtime must be stubbed, "
            "see tests/python/README.md"
        )

    # Both halves of the container runtime seam: the short commands — the
    # preflight, the image lookup, the fetch — and the one that starts a
    # step. A test that stubbed one and not the other would run a real
    # container out of its own assertion.
    monkeypatch.setattr(containerbuild, "run_command", refuse_argv)
    monkeypatch.setattr(containerbuild, "spawn_process", refuse_argv)


#: The environment every scripted registry below answers with, and the
#: one the fixture tree's model resolves against.
ENVIRONMENT_DIGEST = "sha256:" + "ab" * 32
ENVIRONMENT_TAG = "0.1.0-r1"
ENVIRONMENT_REPOSITORY = buildenvironment.ENVIRONMENT_IMAGE_REPOSITORY
ENVIRONMENT_PIN = f"{ENVIRONMENT_REPOSITORY}:{ENVIRONMENT_TAG}@{ENVIRONMENT_DIGEST}"

# --------------------------------------------------------------------------
# The build-environment packages a context v4 pins
# --------------------------------------------------------------------------
#
# Every context this suite creates pins two packages, and every pin has
# to resolve to a hash out of an index — that is the format, and a test
# that shortcut it would be testing a context nothing can build. So the
# package sources these tests write carry three packages, not one: the
# SDK, and the two the SDK's environment lock names.
#
# The tools package here is named WITHOUT an architecture suffix, which
# makes it an ordinary concrete package on every host and keeps the
# fixture free of this machine's architecture. The family-and-meta case
# — the normal one in production — is exercised on purpose in
# test_resolve_pins.py, where the platform is stated rather than
# inherited from whoever runs the suite.

#: The version the SDK archives in this suite carry, and the one their
#: environment lock names for both environment packages.
SDK_VERSION = "0.1.0"
ENVIRONMENT_VERSION = "0.1.0"
WORKSPACE_PACKAGE = "mcuhome-build-workspace"
TOOLS_PACKAGE = "mcuhome-build-tools"

#: The lock document an SDK archive carries — the abstract package set of
#: the build environment specification §5.1, exactly as
#: ``scripts/build_sdk_archive.py`` writes it.
ENVIRONMENT_LOCK = {
    f"packages.{TOOLS_PACKAGE}": ENVIRONMENT_VERSION,
    f"packages.{WORKSPACE_PACKAGE}": ENVIRONMENT_VERSION,
}


def sdk_members(version: str = SDK_VERSION) -> dict[str, tuple[bytes, bool]]:
    """What a minimal but complete SDK archive holds, lock included."""
    import json as _json

    return {
        "mcuhome-sdk.json": (
            b'{"sdk": 1, "generate": {"program": "bin/generate", "runtime": "python3"}}',
            False,
        ),
        "bin/generate": (b"#!/usr/bin/env python3\n", True),
        "mcuhome/model/__init__.py": (f'__version__ = "{version}"\n'.encode(), False),
        "build-environment.lock.json": (
            (_json.dumps(ENVIRONMENT_LOCK, indent=2, sort_keys=True) + "\n").encode(),
            False,
        ),
    }


def build_package_archive(members: dict[str, tuple[bytes, bool]]) -> bytes:
    """A deterministic ``.tar.zst`` of *members* (path -> (bytes, executable))."""
    import io
    import tarfile

    import zstandard

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, (content, executable) in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if executable else 0o644
            tar.addfile(info, io.BytesIO(content))
    return zstandard.ZstdCompressor(level=3).compress(buffer.getvalue())


def make_package_source(directory: Path, *, version: str = SDK_VERSION) -> str:
    """A source directory holding all three packages a context needs.

    The SDK — whose archive carries the environment lock — and the two
    environment packages its lock names. Answers the SDK archive's real
    sha256, which is what a context created against this directory pins.
    """
    import hashlib
    import json as _json

    directory.mkdir(parents=True, exist_ok=True)
    archive = build_package_archive(sdk_members(version))
    filename = f"mcuhome-sdk-{version}.tar.zst"
    (directory / filename).write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    index = {
        "packages": {
            "mcuhome-sdk": {version: {"file": filename, "sha256": digest, "size": len(archive)}}
        }
    }
    write_environment_packages(directory, index)
    (directory / "index.json").write_text(_json.dumps(index), encoding="utf-8")
    return digest


def write_environment_packages(
    directory: Path, index: dict, *, version: str = ENVIRONMENT_VERSION
) -> dict[str, str]:
    """Put the two environment packages into *directory* and *index*.

    The archives are one file each: nothing here unpacks them, the pin
    resolution only ever reads the index, and a test that needs a real
    unpacked tree builds one itself. Answers ``name -> sha256`` so a test
    can assert against the hashes its own context will carry.
    """
    import hashlib

    hashes: dict[str, str] = {}
    for name in (WORKSPACE_PACKAGE, TOOLS_PACKAGE):
        payload = f"{name} {version}\n".encode()
        filename = f"{name}-{version}.tar.zst"
        (directory / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        hashes[name] = digest
        index.setdefault("packages", {})[name] = {
            version: {"file": filename, "sha256": digest, "size": len(payload)}
        }
    return hashes


#: What the two environment packages hash to in this suite. The archives
#: are one deterministic line each (:func:`write_environment_packages`),
#: so the labels a scripted image declares can be computed here instead
#: of being written down twice.
def _package_hash(name: str, version: str = ENVIRONMENT_VERSION) -> str:
    import hashlib

    return hashlib.sha256(f"{name} {version}\n".encode()).hexdigest()


def environment_labels(
    *,
    zephyr: str = "4.4.0",
    generation: str = "3",
    constraint: str = "mcuhome-workbench:",
    workspace: str | None = None,
    tools: str | None = None,
) -> dict[str, str]:
    """The labels an image delivering this suite's package set carries.

    Build-environment specification §5.2: an image mirrors every member
    of the declaration as a label, and the ``packages.`` members carry a
    hash because an image is a delivery. *workspace* and *tools* replace
    a member value outright, which is how a test states the near miss —
    the same package under other bytes.
    """
    labels = {
        f"{buildenvironment.LABEL_PREFIX}spec-generation": generation,
        f"{buildenvironment.LABEL_PREFIX}zephyr.version": zephyr,
        f"{buildenvironment.LABEL_PREFIX}build-context.generator-constraint": constraint,
        f"{buildenvironment.LABEL_PREFIX}packages.{WORKSPACE_PACKAGE}": (
            workspace
            if workspace is not None
            else f"{ENVIRONMENT_VERSION}@sha256:{_package_hash(WORKSPACE_PACKAGE)}"
        ),
        f"{buildenvironment.LABEL_PREFIX}packages.{TOOLS_PACKAGE}": (
            tools
            if tools is not None
            else f"{ENVIRONMENT_VERSION}@sha256:{_package_hash(TOOLS_PACKAGE)}"
        ),
    }
    return {name: value for name, value in labels.items() if value}


class ScriptedRegistry:
    """A container registry that answers with one image, and counts the asking.

    Passed as ``images=`` wherever a build resolves an environment. It is
    not a convenience: choosing an image is the one step of a container
    build that talks to the network, and a suite whose promise is one
    second may not.

    The labels are the whole answer. An image is chosen by declaring
    exactly the package set the context pinned (§5.2), so a test moves
    the *labels* to move the outcome: another Zephyr release, another
    hash on a package, a member missing.
    """

    def __init__(
        self,
        *,
        digest: str = ENVIRONMENT_DIGEST,
        tag: str = ENVIRONMENT_TAG,
        tags: tuple[str, ...] | None = None,
        zephyr: str = "4.4.0",
        generation: str = "3",
        constraint: str = "mcuhome-workbench:",
        workspace: str | None = None,
        tools: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.digest = digest
        self.tag = tag
        self.tags_ = tags if tags is not None else (tag,)
        self.labels_ = (
            labels
            if labels is not None
            else environment_labels(
                zephyr=zephyr,
                generation=generation,
                constraint=constraint,
                workspace=workspace,
                tools=tools,
            )
        )
        self.asked: list[str] = []

    def tags(self, reference):
        """What the repository publishes, in the order the registry lists it."""
        del reference
        return self.tags_

    def facts(self, reference, *, platform=None):
        del platform  # one architecture is enough for a suite about builds
        self.asked.append(reference.tag or "")
        from mcuhome.workbench.ociregistry import ImageFacts

        return ImageFacts(digest=self.digest, labels=dict(self.labels_))


@pytest.fixture(autouse=True)
def _no_registry(monkeypatch):
    """Nothing in this suite is allowed to reach a container registry.

    The same safety net as :func:`_no_docker` and for the same reason:
    resolving a build environment is now part of an ordinary build, it
    goes over HTTPS, and a test that forgot to pass a scripted registry
    would otherwise quietly depend on ghcr.io being up and on what is
    published there today. Tests that want a registry pass one.
    """

    def refuse(self, url, headers, timeout):
        del headers, timeout
        raise AssertionError(
            f"a test tried to reach {url}: pass registry=ScriptedRegistry() "
            "wherever a build environment is resolved"
        )

    monkeypatch.setattr(ociregistry.Registry, "_urlopen", refuse)


# --- resolving a configuration (stages 1-3) --------------------------
#
# The repository split ran through this file and everything from here
# down is what stayed: resolving a configuration is stages 1-3, which is
# `mcuhome.workbench`. The half that travelled took the context writer
# and the golden-model reader with it; what those two repositories still
# share is `data/golden/00-bmp180-two-endpoints.device-model.json` —
# pinned here against the real resolver (`test_model_golden.py`) and read
# there as the model itself.

FIXTURE_TREE = DATA_DIR / "tree"

#: A configuration that passes every check, used as the baseline the
#: gate tests break one thing at a time.
VALID_CONFIG = """\
device:
  name: bench-node
  board: nrf7002dk/nrf5340/cpuapp

network:
  thread:
    device_role: ftd
  matter:
    enabled: true
    use_test_pairing: true

hardware:
  buses:
    i2c0:
      controller: arduino_i2c
  peripherals:
    baro:
      driver: bosch,bmp180
      bus: i2c0

node:
  endpoints:
    - id: 1
      device_type: temperature_sensor
      clusters:
        temperature_measurement:
          source: baro.temperature
          sampling: 10s
"""


def line_of(text: str, needle: str) -> int:
    """1-based line number of the first line containing *needle*."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in the configuration")


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a configuration into a throwaway project and return its path."""

    def write(text: str, *, name: str = "main.yaml", secrets: str | None = None) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        if secrets is not None:
            secrets_file = tmp_path / "secrets" / "main.yaml"
            secrets_file.parent.mkdir(mode=0o700, exist_ok=True)
            secrets_file.write_text(secrets, encoding="utf-8")
            secrets_file.chmod(0o600)
        return path

    return write


def resolve_file(path: Path) -> DeviceModel:
    """Run stages 1-3 on a configuration file, project discovery included."""
    root = find_project_root(path.parent)
    project = Project(root=root or path.parent, discovered=root is not None)
    return load_model(path, project=project)


def errors_of(exc: ConfigError | ConfigErrorGroup) -> list[ConfigError]:
    """Flatten a single error or an error group into a list."""
    if isinstance(exc, ConfigErrorGroup):
        return exc.errors
    return [exc]


def expect_failure(path: Path) -> list[ConfigError]:
    """Resolve *path*, expecting it to be rejected, and return the errors."""
    with pytest.raises((ConfigError, ConfigErrorGroup)) as caught:
        resolve_file(path)
    return errors_of(caught.value)


def find_error(errors: list[ConfigError], fragment: str) -> ConfigError:
    """The one error whose message contains *fragment*."""
    matches = [error for error in errors if fragment in error.message]
    assert matches, f"no error mentioning {fragment!r}; got: " + "; ".join(
        error.message for error in errors
    )
    assert len(matches) == 1, f"{fragment!r} matched {len(matches)} errors"
    return matches[0]
