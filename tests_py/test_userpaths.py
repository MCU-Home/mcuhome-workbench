# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Per-user directories come from the stated environment, not the process.

Two things are pinned here. The first is what :mod:`mcuhome.userpaths`
answers. The second is bigger than that module: **no other module in the
package may read process state at all** — ADR 0020 turns this library
into one process serving several sessions, and a call-time read of
``os.environ``, ``Path.home()`` or ``Path.cwd()`` is what makes two
sessions in one process answer each other's questions.

That invariant reads the syntax tree rather than the text, unlike the
identity and registry invariants next door. Those forbid a *name* from
appearing anywhere, comments included, because a second spelling of a
Kconfig symbol is the hazard whatever surrounds it. Here the hazard is a
real access: a module that explains in prose why it takes ``env`` instead
of reading ``os.environ`` is doing the right thing, and a text search
would fail it for saying so.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import package_modules

from mcuhome import userpaths
from mcuhome.errors import ConfigError

# --------------------------------------------------------------------------
# What it answers
# --------------------------------------------------------------------------


def test_the_home_directory_is_the_one_the_environment_names(tmp_path) -> None:
    assert userpaths.home({"HOME": str(tmp_path / "someone")}) == tmp_path / "someone"


def test_an_environment_without_a_home_directory_is_refused(monkeypatch, tmp_path) -> None:
    """Refused, not guessed — the directory in question holds a private key.

    The process is given a perfectly good ``HOME`` here, which is exactly
    the answer that must not come out.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "the-process"))
    with pytest.raises(ConfigError) as caught:
        userpaths.home({})
    assert "HOME" in str(caught.value)


def test_a_path_without_a_tilde_comes_back_untouched(tmp_path) -> None:
    assert userpaths.expand("/etc/mcuhome/signing.key", {}) == Path("/etc/mcuhome/signing.key")
    assert userpaths.expand(tmp_path / "rel", {}) == tmp_path / "rel"


def test_a_leading_tilde_is_the_environments_home_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "the-process"))
    env = {"HOME": str(tmp_path / "the-caller")}
    assert userpaths.expand("~/keys/mine.key", env) == tmp_path / "the-caller" / "keys" / "mine.key"


def test_a_bare_tilde_is_the_home_directory_itself(tmp_path) -> None:
    assert userpaths.expand("~", {"HOME": str(tmp_path)}) == tmp_path


def test_a_named_account_is_not_an_environment_question(tmp_path) -> None:
    """``~root`` names an account, and only the account database answers it.

    Whatever ``HOME`` says, it does not say where *root* lives, so this
    form is handed to :meth:`~pathlib.Path.expanduser` — which is why the
    invariant below allows that one call site.
    """
    env = {"HOME": str(tmp_path)}
    assert userpaths.expand("~root/keys", env) != tmp_path / "root" / "keys"


# --------------------------------------------------------------------------
# What no module may do
# --------------------------------------------------------------------------

#: ``attribute name`` → what to use instead. ``expanduser`` is on the list
#: because it reads ``HOME`` out of the process just as ``Path.home()``
#: does; the ``~user`` form in :mod:`mcuhome.userpaths` is the exception
#: that proves it, and that module is excluded wholesale.
FORBIDDEN_ATTRIBUTES = {
    "environ": "take an env: dict[str, str] argument",
    "getenv": "take an env: dict[str, str] argument",
    "getcwd": "take the directory as an argument",
    "cwd": "take the directory as an argument",
    "home": "mcuhome.userpaths.home(env)",
    "expanduser": "mcuhome.userpaths.expand(path, env)",
}

#: Modules that may do it. One, and its reason is its docstring.
EXEMPT = {"userpaths.py"}

#: The modules these names mean something dangerous in. ``from
#: mcuhome.userpaths import home`` is the *fix*, not the defect, so the
#: import check asks where a name comes from rather than what it is.
PROCESS_MODULES = {"os", "os.path", "pathlib", "posixpath", "ntpath"}


def _process_reads(source: str) -> list[str]:
    """Every access in *source* that reaches the process for an answer."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module in PROCESS_MODULES:
            found.extend(a.name for a in node.names if a.name in FORBIDDEN_ATTRIBUTES)
    return found


def test_no_module_reads_process_state() -> None:
    """One process, several sessions, one environment each (ADR 0020).

    ``signing.py`` and ``container.py`` are asserted to be among the
    modules examined because both used to hold exactly this read — a
    search that stopped reaching them would pass while checking less than
    it did yesterday.
    """
    modules = package_modules()
    names = {path.name for path in modules}
    assert {"signing.py", "container.py"} <= names, (
        "the search no longer reaches the modules that used to read the "
        "process — extend conftest.PACKAGES"
    )
    for module in modules:
        if module.name in EXEMPT:
            continue
        reads = _process_reads(module.read_text(encoding="utf-8"))
        assert not reads, (
            f"{module.name} reaches the process for {sorted(set(reads))}; "
            f"use {FORBIDDEN_ATTRIBUTES[reads[0]]} instead"
        )
