# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The fixed value sets this package publishes, against what it does.

A client renders origins, statuses, package kinds and protocol verbs off
these tuples, so each one has to be the set the code actually uses — a
vocabulary that drifts from the values it names is worse than none,
because a client cannot tell that it has.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from conftest import package_modules

from mcuhome.workbench import sessionclient
from mcuhome.workbench.buildenvsession import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    STEP_STATUSES,
)
from mcuhome.workbench.buildenvstore import EXTRACTION_BOUNDS
from mcuhome.workbench.configuration import CONFIG_ORIGINS, OPTIONS, resolve_settings
from mcuhome.workbench.diagnostics import WARNING_KINDS, Diagnostic
from mcuhome.workbench.resolve_pins import KIND_SDK, KIND_TOOLS, KIND_WORKSPACE, PACKAGE_KINDS


def test_the_origins_are_the_layers_a_resolution_can_name() -> None:
    """Every origin a Setting can carry is in the published set."""
    assert CONFIG_ORIGINS[0] == "default"
    assert CONFIG_ORIGINS[-1] == "arguments"
    settings = resolve_settings(project=None, env={"MCUHOME_BUILD_MODE": "subprocess"})
    assert settings.origin("build.mode") in CONFIG_ORIGINS
    for option in OPTIONS:
        if option.bootstrap:
            continue
        assert settings.origin(option.name) in CONFIG_ORIGINS


def test_the_step_statuses_are_the_three_members_beside_them() -> None:
    assert STEP_STATUSES == (STATUS_SUCCESS, STATUS_FAILURE, STATUS_UNSUPPORTED)


def test_the_package_kinds_are_the_kinds_the_store_holds_bounds_for() -> None:
    assert PACKAGE_KINDS == (KIND_SDK, KIND_WORKSPACE, KIND_TOOLS)
    assert set(EXTRACTION_BOUNDS) == set(PACKAGE_KINDS)


def test_every_session_verb_is_one_this_client_speaks() -> None:
    """The eleven verbs, against the module that sends them.

    The client names each verb in the call that sends it, so a verb this
    tuple carries and the client does not send — or one it sends under a
    spelling nobody published — shows up here rather than in a build
    server's rejection.
    """
    source = inspect.getsource(sessionclient)
    assert len(sessionclient.SESSION_VERBS) == 11
    assert len(set(sessionclient.SESSION_VERBS)) == 11
    for verb in sessionclient.SESSION_VERBS:
        assert f'"{verb}"' in source, verb


def _reported_warning_kinds() -> set[str]:
    """The *kind* every ``Diagnostic.warning`` call in the package states.

    Read out of the syntax rather than by running the package: a warning
    is reported from a branch a test has to arrange for, and the kinds
    are a published set whether or not today's suite reaches every one
    of those branches.
    """
    found: set[str] = set()
    for path in package_modules():
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            if not isinstance(callee, ast.Attribute) or callee.attr != "warning":
                continue
            if not isinstance(callee.value, ast.Name) or callee.value.id != "Diagnostic":
                continue
            for keyword in node.keywords:
                if keyword.arg == "kind" and isinstance(keyword.value, ast.Constant):
                    found.add(str(keyword.value.value))
    return found


def test_every_warning_kind_is_one_this_package_reports() -> None:
    """The published set, against the warnings that are actually written.

    Both directions: a kind nothing reports is a promise to a client that
    nothing keeps, and a kind reported without being published would
    arrive at a client that cannot look it up. The second direction is
    refused at runtime as well — ``Diagnostic.warning`` checks the value
    — but a literal in a rarely taken branch would only be caught the day
    that branch runs.
    """
    assert _reported_warning_kinds() == set(WARNING_KINDS)


def test_a_kind_nobody_published_is_refused_where_it_is_written() -> None:
    """The guard that keeps the set worth switching on.

    A client looks a kind up. One that never appeared in the reference
    would arrive as a value it cannot resolve — so the mistake is
    refused where it is made, in this package, rather than delivered.
    It is a ``ValueError`` and not a user-facing refusal: nobody outside
    this package writes a warning.
    """
    with pytest.raises(ValueError, match="WARNING_KINDS") as caught:
        Diagnostic.warning("something", kind="unlisted_kind")
    assert "unlisted_kind" in str(caught.value)
    for kind in WARNING_KINDS:
        assert Diagnostic.warning("something", kind=kind).severity == "warning"


def test_the_warning_kinds_are_spelled_the_way_the_scheme_says() -> None:
    """Lowercase with underscores, and no duplicates."""
    assert len(set(WARNING_KINDS)) == len(WARNING_KINDS)
    for kind in WARNING_KINDS:
        assert kind == kind.lower()
        assert kind.replace("_", "").isalnum()
