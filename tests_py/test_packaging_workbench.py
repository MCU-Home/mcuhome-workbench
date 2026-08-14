# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``mcuhome-workbench`` distribution and its ``remote`` extra.

The workbench half of the packaging invariants; what holds the three
distributions of ADR 0020 together as a set — the partition, the one
shared version, the dependency arrows — is in ``test_packaging.py``.

These two tests live in a packaging file, and not beside the
session-client tests they are about, for one reason: that file is gated
on the extra plus a sibling checkout of the build server, and a guard
against a silently dropped extra is worth nothing in the only
environment that would notice — the one where the extra is not
installed. They need neither dependency (``sessionclient`` imports
nothing optional at module level), so they belong where the rest of the
packaging invariants are.
"""

from __future__ import annotations

import tomllib

import pytest
from conftest import REPO_ROOT

PACKAGING_DIR = REPO_ROOT / "packaging"


def _project_files() -> dict[str, dict]:
    """Every distribution's parsed ``pyproject.toml``, by directory name."""
    found = {
        path.parent.name: tomllib.loads(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGING_DIR.glob("*/pyproject.toml"))
    }
    assert found, f"no project files under {PACKAGING_DIR}"
    return found


def test_a_missing_extra_is_a_refusal_that_names_the_install() -> None:
    """The refusal a caller without the extra gets, worded like the rest.

    ``mcuhome.compiler.localbuild`` refuses a missing docker by stating
    the fact and naming the fix; a missing transport is the same kind of
    news, and an ``ImportError`` traceback is not.
    """
    from mcuhome.workbench import sessionclient as sc

    with pytest.raises(sc.RemoteDependencyMissing) as refusal:
        sc._require("mcuhome_nothing_like_this", "the test asked for a module nobody has")
    rendered = str(refusal.value)
    assert "pip install 'mcuhome-workbench[remote]'" in rendered
    assert "mcuhome_nothing_like_this" in rendered


def test_the_remote_extra_is_declared_in_the_distribution() -> None:
    """The extra the refusal above names is the extra packaging declares.

    A refusal that names an extra nobody declared sends a user to a
    command that does nothing, so the message and the declaration are
    checked against each other rather than each against a reader's
    memory.

    Read out of ``packaging/workbench/setup.py`` rather than out of
    ``importlib.metadata``: an editable install's metadata is as fresh as
    the last ``pip install -e``, so a metadata read would pass or fail on
    when somebody last ran one instead of on what the distribution
    declares.
    """
    source = (PACKAGING_DIR / "workbench" / "setup.py").read_text(encoding="utf-8")
    assert "extras_require" in source
    assert '"remote": ["aiohttp>=3.10,<4", "zstandard>=0.22"]' in source

    # And the half that is easy to get silently wrong: `dynamic` is per
    # field, so an `extras_require` in setup.py whose field is not listed
    # in pyproject.toml is *discarded* by setuptools — the extra would
    # then install nothing while `pip` reported success.
    project = _project_files()["workbench"]["project"]
    assert "optional-dependencies" in project["dynamic"]
    assert "optional-dependencies" not in project
