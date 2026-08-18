# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers for the workbench tests.

Everything here is the tools repository's half of what used to be one
suite. :mod:`mcuhome.model` and :mod:`mcuhome.compiler` moved to
``mcuhome-sdk`` with ADR 0024 and are *installed dependencies* now, not
sources in this tree — so they are imported freely and never searched.
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
from mcuhome.model.errors import ConfigError, ConfigErrorGroup
from mcuhome.model.model import DeviceModel

from mcuhome.workbench import buildenv as container
from mcuhome.workbench.api import load_model
from mcuhome.workbench.project import Project, find_project_root

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
DATA_DIR = TESTS_DIR / "data"
EXAMPLES_DIR = DATA_DIR / "examples"
GOLDEN_DIR = DATA_DIR / "golden"

#: The import package the distributions of ADR 0020 share, and the
#: directory it is assembled from in this checkout. It is a PEP 420
#: namespace package, which is why the directory is named here at all:
#: the import system cannot enumerate one. ``find_spec("mcuhome")``
#: answers with ``origin is None`` and a search-location list that holds
#: path-hook tokens rather than directories under an editable install, so
#: "what is under the namespace" is a question about the tree.
NAMESPACE = "mcuhome"
NAMESPACE_DIR = REPO_ROOT / NAMESPACE

#: The packages the whole-package invariant searches must cover — the one
#: distribution of ADR 0020 decision 1 this repository ships, by import
#: name. ``mcuhome.model`` and ``mcuhome.compiler`` are the SDK
#: repository's since ADR 0024 and are covered by the same searches
#: there; a copy of either appearing in this tree would be the defect,
#: which is what the enumeration in :func:`package_modules` catches.
PACKAGES = ("mcuhome.workbench",)


def package_modules() -> list[Path]:
    """Every ``.py`` file of every package the invariants have to cover.

    Derived from the importable packages rather than from one module's
    directory. A directory glob reads "every module there is" only while
    there is one package; after the ADR 0020 split it would keep passing
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
      ADR 0024 it does a third job — a leftover ``mcuhome/model/`` or
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

    The key lives per project since ADR 0022 (``secrets/firmware/
    mcuboot.yaml``, ADR 0015 decision 8), but ``MCUHOME_SIGNING_KEY``
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
    stopped reading the process — ``tests_py/test_userpaths_workbench.py``
    proves it for every module — so nothing here resolves a key out of the
    environment pytest happens to run in. What is left for this fixture
    is everything that hands the process environment *in*: the command
    line's ``env=os.environ``, and any test that does the same.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("MCUHOME_SIGNING_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    """Nothing in this suite is allowed to reach a container runtime.

    A safety net, not a convenience: `mcuhome build` now defaults to the
    container, so a test that forgets to stub stage 5 would otherwise
    quietly start a real Matter build on the machine running pytest —
    minutes of CPU and gigabytes of build directory, from a suite whose
    whole promise is one second. Tests that want a working preflight
    replace this with their own runner, which wins because their
    monkeypatch is applied later.
    """

    def refuse(command, env):
        raise AssertionError(
            f"a test tried to run {command[0]!r}: stage 5 must be stubbed, see tests_py/README.md"
        )

    monkeypatch.setattr(container, "_run_quiet", refuse)


# --- resolving a configuration (stages 1-3) --------------------------
#
# The cut of ADR 0024 ran through this file and everything from here down
# is what stayed: resolving a configuration is stages 1-3, which is
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
