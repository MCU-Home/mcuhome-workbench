# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The fixed value sets this package publishes, against what it does.

A client renders origins, statuses, package kinds and protocol verbs off
these tuples, so each one has to be the set the code actually uses — a
vocabulary that drifts from the values it names is worse than none,
because a client cannot tell that it has.
"""

from __future__ import annotations

import inspect

from mcuhome.workbench import sessionclient
from mcuhome.workbench.buildenvsession import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    STEP_STATUSES,
)
from mcuhome.workbench.buildenvstore import EXTRACTION_BOUNDS
from mcuhome.workbench.configuration import CONFIG_ORIGINS, OPTIONS, resolve_settings
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
