# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers for the builder tests.

Arranged in two halves, and the order is the point. Everything above the
marked block near the bottom needs nothing but :mod:`mcuhome.model` and
:mod:`mcuhome.compiler`; everything below it needs
:mod:`mcuhome.workbench`. ADR 0024 moves the first two into a repository
of their own, so the marker is where this file gets cut — not a tidiness
convention but the seam, kept visible while both halves still live here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from mcuhome.compiler import container
from mcuhome.model.errors import ConfigError, ConfigErrorGroup
from mcuhome.model.model import DeviceModel

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
EXAMPLES_DIR = REPO_ROOT / "docs" / "design" / "examples"
DATA_DIR = TESTS_DIR / "data"
GOLDEN_DIR = DATA_DIR / "golden"

#: The import package the three distributions of ADR 0020 share, and the
#: directory it is assembled from in this checkout. It is a PEP 420
#: namespace package, which is why the directory is named here at all:
#: the import system cannot enumerate one. ``find_spec("mcuhome")``
#: answers with ``origin is None`` and a search-location list that holds
#: path-hook tokens rather than directories under an editable install, so
#: "what is under the namespace" is a question about the tree.
NAMESPACE = "mcuhome"
NAMESPACE_DIR = REPO_ROOT / NAMESPACE

#: The packages the whole-package invariant searches must cover — the
#: three distributions of ADR 0020 decision 1, by import name.
#: :func:`package_modules` checks this list against what is actually in
#: :data:`NAMESPACE_DIR`, so a fourth subpackage cannot arrive unsearched.
PACKAGES = ("mcuhome.compiler", "mcuhome.model", "mcuhome.workbench")


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
      would have to belong to one of three distributions that all deliver
      into that directory, and PEP 420 forbids it for exactly that reason.
    * No module sits directly under the namespace directory. Such a file
      is in no distribution, ships with none of the three, and is
      invisible to every search below.
    * :data:`PACKAGES` lists every subpackage there is, and each one is
      imported *from this checkout*. The second half matters as much as
      the first: against a non-editable install the searches would read
      copies in ``site-packages`` while the tests exercise the tree.
    """
    spec = importlib.util.find_spec(NAMESPACE)
    assert spec is not None, f"{NAMESPACE} is not importable"
    assert spec.origin is None, (
        f"{NAMESPACE} has become a regular package (origin={spec.origin}). "
        "PEP 420 forbids an __init__.py there — three distributions deliver "
        "into that directory and only one of them could own the file."
    )

    loose = sorted(path.name for path in NAMESPACE_DIR.glob("*.py"))
    assert not loose, (
        f"{loose} sit directly under {NAMESPACE_DIR} — no distribution "
        "ships them and no invariant searches them"
    )
    subpackages = {
        path.name for path in NAMESPACE_DIR.iterdir() if (path / "__init__.py").is_file()
    }
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

    ``mcuhome build`` generates one on first need under
    ``$XDG_CONFIG_HOME/mcuhome/`` (ADR 0015 decision 8), which on the
    machine running this suite is a real, long-lived private key. A test
    that reaches it would either read a secret it has no business
    reading or — worse — create one silently outside a temporary
    directory. Point the variables at the test's own tmp_path instead;
    tests that care about the resolution rules pass an explicit ``env``.

    ``HOME`` is redirected as well, and not for symmetry: without
    ``XDG_CONFIG_HOME`` the key sits under ``~/.config``, so the two
    variables are two names for the same directory and covering one of
    them covers half the paths that lead there.

    **What this fixture no longer has to catch.** The package itself
    stopped reading the process — ``tests_py/test_userpaths.py`` proves
    it for every module — so nothing here resolves a key out of the
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


# --- workbench-side fixtures (leave in the tools repo at the split) ---
#
# Everything below this line resolves a configuration, and resolving is
# stages 1-3, which is `mcuhome.workbench`. ADR 0024 leaves the workbench
# where it is and moves model+compiler out, so the cut is exactly here:
# the half above travels, this half stays, and the two imports that make
# the difference are the first thing in the block rather than mixed into
# the header (E402 is the price of saying so in one place).
#
# `ConfigError`, `ConfigErrorGroup` and `DeviceModel` are imported at the
# top because they are `mcuhome.model`, which both repositories depend
# on; after the cut they are used only here, so the departing half drops
# them.

from mcuhome.workbench.api import load_model  # noqa: E402
from mcuhome.workbench.tree import ConfigTree, find_config_root  # noqa: E402

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
    """Write a configuration into a throwaway tree and return its path."""

    def write(text: str, *, name: str = "main.yaml", secrets: str | None = None) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        if secrets is not None:
            (tmp_path / "secrets.yaml").write_text(secrets, encoding="utf-8")
        return path

    return write


def resolve_file(path: Path) -> DeviceModel:
    """Run stages 1-3 on a configuration file, tree discovery included."""
    root = find_config_root(path.parent)
    tree = ConfigTree(root=root or path.parent, discovered=root is not None)
    return load_model(path, tree=tree)


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
