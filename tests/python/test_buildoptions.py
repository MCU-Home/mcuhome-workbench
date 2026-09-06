# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""From the ``build`` configuration section to what a build actually does.

:mod:`test_configuration_build` asserts that the keys resolve; this file
asserts that the resolved values *arrive*: at the target a method
produces, at the provisioner's store and interpreter, at the unpacking
bounds, at the per-package source directories and at the compiler cache
tiers. Nothing here compiles anything — every consumer is stubbed at the
seam that would do work, and what is checked is the argument it was
handed.

The two rules the wiring follows are asserted as well, because both are
easy to get backwards: a value the *request* states wins over the
configuration (a caller that named something meant it), and a request
that states nothing gets the machine's own answer without the caller
having to know these keys exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, resolve_file
from mcuhome.model.errors import ConfigError

from mcuhome.workbench import buildenvstore, buildmethods, subprocessbuild
from mcuhome.workbench.buildmethods import BuildOptions, BuildRequest, build_options, options_for
from mcuhome.workbench.configuration import resolve_settings
from mcuhome.workbench.project import Project, init_project


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return init_project(tmp_path / "project").project


def configured(project: Project, text: str) -> BuildOptions:
    project.config_file.write_text(text, encoding="utf-8")
    return build_options(resolve_settings(project=project, env={}))


# --------------------------------------------------------------------------
# The section, as one object
# --------------------------------------------------------------------------


def test_nothing_configured_is_the_machine_that_was_never_touched() -> None:
    options = BuildOptions()
    assert options.mode == buildmethods.MODE_CONTAINER
    assert options.env_store is None
    assert options.python is None
    assert options.workspace_sources == ()
    assert options.bound(buildenvstore.WORKSPACE_KIND) is None


def test_every_key_reaches_its_field(project: Project) -> None:
    options = configured(
        project,
        "build:\n"
        "  mode: subprocess\n"
        "  env_store: /srv/store\n"
        "  dev_workspace: /dev/workspace\n"
        "  dev_tools: /dev/tools\n"
        "  python: python3.13\n"
        "  workspace_sources:\n    - /srv/workspaces\n"
        "  tools_sources:\n    - /srv/tools\n"
        "  sdk_max_bytes: 11\n"
        "  workspace_max_bytes: 22\n"
        "  tools_max_bytes: 33\n"
        "  cache_local: /srv/cache/local\n"
        "  cache_shared: /srv/cache/shared\n"
        "  cache_session: /srv/cache/session\n"
        "  cache_project: /srv/cache/project\n",
    )
    assert options.mode == buildmethods.MODE_SUBPROCESS
    assert options.mode_source == str(project.config_file)
    assert options.env_store == Path("/srv/store")
    assert options.dev_workspace == Path("/dev/workspace")
    assert options.dev_tools == Path("/dev/tools")
    assert options.python == "python3.13"
    assert options.workspace_sources == (Path("/srv/workspaces"),)
    assert options.tools_sources == (Path("/srv/tools"),)
    assert options.bound(buildenvstore.SDK_KIND) == 11
    assert options.bound(buildenvstore.WORKSPACE_KIND) == 22
    assert options.bound(buildenvstore.TOOLS_KIND) == 33
    assert options.cache_local == Path("/srv/cache/local")
    assert options.cache_shared == Path("/srv/cache/shared")
    assert options.cache_session == Path("/srv/cache/session")
    assert options.cache_project == Path("/srv/cache/project")


def test_a_bound_nobody_set_is_not_a_statement(project: Project) -> None:
    """The store's own table answers, and is not restated as a decision."""
    options = configured(project, "build:\n  mode: subprocess\n")
    assert options.bound(buildenvstore.WORKSPACE_KIND) is None
    # A value equal to the default is still a statement when a layer made it.
    stated = configured(
        project,
        f"build:\n  workspace_max_bytes: "
        f"{buildenvstore.EXTRACTION_BOUNDS[buildenvstore.WORKSPACE_KIND]}\n",
    )
    assert (
        stated.bound(buildenvstore.WORKSPACE_KIND)
        == (buildenvstore.EXTRACTION_BOUNDS[buildenvstore.WORKSPACE_KIND])
    )


def test_a_request_that_states_options_is_answered_with_them(model, tmp_path) -> None:
    stated = BuildOptions(mode=buildmethods.MODE_SUBPROCESS, python="python3.13")
    assert options_for(BuildRequest(model=model, out_dir=tmp_path, options=stated)) is stated


def test_a_request_without_options_reads_the_machines_configuration(
    model, tmp_path, project: Project
) -> None:
    project.config_file.write_text("build:\n  mode: subprocess\n", encoding="utf-8")
    options = options_for(
        BuildRequest(model=model, out_dir=tmp_path, project_root=project.root, env={})
    )
    assert options.mode == buildmethods.MODE_SUBPROCESS


def test_the_environment_reaches_a_request_without_a_project(model, tmp_path) -> None:
    options = options_for(
        BuildRequest(
            model=model,
            out_dir=tmp_path,
            env={"MCUHOME_BUILD_MODE": "subprocess", "MCUHOME_BUILD_PYTHON": "python3.13"},
        )
    )
    assert options.mode == buildmethods.MODE_SUBPROCESS
    assert options.python == "python3.13"


# --------------------------------------------------------------------------
# The target: mode and the developer trees
# --------------------------------------------------------------------------


def test_the_configured_mode_selects_the_execution(model, tmp_path) -> None:
    target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        BuildRequest(
            model=model,
            out_dir=tmp_path,
            options=BuildOptions(mode=buildmethods.MODE_SUBPROCESS),
        ),
    )
    assert isinstance(target.execution, buildmethods.SubprocessExecution)


def test_a_stated_mode_beats_the_configuration(model, tmp_path) -> None:
    target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        BuildRequest(
            model=model,
            out_dir=tmp_path,
            build_mode=buildmethods.MODE_CONTAINER,
            options=BuildOptions(mode=buildmethods.MODE_SUBPROCESS),
        ),
    )
    assert isinstance(target.execution, buildmethods.ContainerExecution)


def test_the_configured_developer_trees_reach_the_execution(model, tmp_path) -> None:
    target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        BuildRequest(
            model=model,
            out_dir=tmp_path,
            options=BuildOptions(
                mode=buildmethods.MODE_SUBPROCESS,
                dev_workspace=tmp_path / "workspace",
                dev_tools=tmp_path / "tools",
            ),
        ),
    )
    assert target.execution.dev_workspace == tmp_path / "workspace"
    assert target.execution.dev_tools == tmp_path / "tools"


def test_a_stated_developer_tree_beats_the_configured_one(model, tmp_path) -> None:
    target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        BuildRequest(
            model=model,
            out_dir=tmp_path,
            dev_workspace=tmp_path / "stated",
            options=BuildOptions(
                mode=buildmethods.MODE_SUBPROCESS,
                dev_workspace=tmp_path / "configured",
                dev_tools=tmp_path / "tools",
            ),
        ),
    )
    assert target.execution.dev_workspace == tmp_path / "stated"
    assert target.execution.dev_tools == tmp_path / "tools"


# --------------------------------------------------------------------------
# An image named for a build that starts no container
# --------------------------------------------------------------------------


def test_an_image_without_a_container_is_refused_naming_both_ways_out(
    model, tmp_path, project: Project
) -> None:
    """The two statements contradict each other, and neither is honoured halfway."""
    project.config_file.write_text("build:\n  mode: subprocess\n", encoding="utf-8")
    request = BuildRequest(
        model=model,
        out_dir=tmp_path,
        project_root=project.root,
        image="ghcr.io/mcu-home/build-environment:0.1.10.dev1-r1",
    )
    with pytest.raises(ConfigError) as refusal:
        buildmethods.target_for_method(buildmethods.LOCAL, request)
    rendered = str(refusal.value)
    hint = refusal.value.hint or ""
    assert "ghcr.io/mcu-home/build-environment:0.1.10.dev1-r1" in rendered
    assert buildmethods.MODE_SUBPROCESS in hint
    # It says where the mode came from, because that is usually a file
    # the person running the build is not looking at.
    assert str(project.config_file) in hint
    assert f"build.mode {buildmethods.MODE_CONTAINER}" in hint


def test_a_mode_this_build_stated_is_not_blamed_on_a_file(model, tmp_path) -> None:
    """The refusal names whoever chose the mode, and a mode stated for one
    build came from no file."""
    with pytest.raises(ConfigError) as refusal:
        buildmethods.target_for_method(
            buildmethods.LOCAL,
            BuildRequest(
                model=model,
                out_dir=tmp_path,
                build_mode=buildmethods.MODE_SUBPROCESS,
                image="ghcr.io/mcu-home/x:1",
                options=BuildOptions(mode_source="/etc/mcuhome/configuration.yaml"),
            ),
        )
    hint = refusal.value.hint or ""
    assert "from this build" in hint
    assert "/etc/mcuhome/configuration.yaml" not in hint


def test_an_image_with_a_container_is_the_ordinary_case(model, tmp_path) -> None:
    target = buildmethods.target_for_method(
        buildmethods.LOCAL,
        BuildRequest(model=model, out_dir=tmp_path, image="ghcr.io/mcu-home/x:1"),
    )
    assert target.execution.image == "ghcr.io/mcu-home/x:1"


# --------------------------------------------------------------------------
# The provisioner: store, interpreter, bounds, per-package sources
# --------------------------------------------------------------------------


def test_the_options_reach_the_provisioner_and_the_backend(model, tmp_path, monkeypatch) -> None:
    """One composition, and every configured value lands where it is used."""
    provisioned: dict[str, object] = {}
    driven: dict[str, object] = {}

    def fake_environment_from_pins(pin, **kwargs):
        provisioned.update(kwargs)
        return "an environment"

    def fake_run_locked_build(context_dir, **kwargs):
        driven.update(kwargs)
        driven["context_dir"] = context_dir
        return "a result"

    monkeypatch.setattr(subprocessbuild, "environment_from_pins", fake_environment_from_pins)
    monkeypatch.setattr(subprocessbuild, "run_locked_build", fake_run_locked_build)
    monkeypatch.setattr(subprocessbuild, "refuse_patched_context", lambda *a, **k: None)
    monkeypatch.setattr(subprocessbuild, "check_environment", lambda *a, **k: None)
    monkeypatch.setattr(buildmethods, "lock_context", lambda directory: None)
    monkeypatch.setattr(
        buildmethods,
        "read_context_request",
        lambda path: type("Request", (), {"build_environment": "the pin"})(),
    )
    monkeypatch.setattr(buildmethods, "format_generator_chain", lambda entries: "mcuhome 0.1")
    monkeypatch.setattr(buildmethods, "read_generator_chain", lambda path: ())

    options = BuildOptions(
        mode=buildmethods.MODE_SUBPROCESS,
        env_store=tmp_path / "store",
        python="python3.13",
        workspace_sources=(tmp_path / "workspaces",),
        tools_sources=(tmp_path / "tools",),
        sdk_max_bytes=11,
        workspace_max_bytes=22,
        tools_max_bytes=33,
        cache_local=tmp_path / "cache-local",
        cache_shared=tmp_path / "cache-shared",
        cache_session=tmp_path / "cache-session",
        cache_project=tmp_path / "cache-project",
    )
    (tmp_path / "cache-shared").mkdir()
    result = buildmethods.compose_subprocess_build(
        model,
        sdk_sources=(tmp_path / "sdk",),
        work_root=tmp_path / "work",
        env={},
        context_dir=tmp_path / "context",
        options=options,
    )
    assert result == "a result"

    assert provisioned["store"] == tmp_path / "store"
    assert provisioned["interpreter"] == "python3.13"
    assert provisioned["workspace_sources"] == (tmp_path / "workspaces",)
    assert provisioned["tools_sources"] == (tmp_path / "tools",)
    assert provisioned["bounds"] == {
        buildenvstore.WORKSPACE_KIND: 22,
        buildenvstore.TOOLS_KIND: 33,
    }

    assert driven["sdk_max_bytes"] == 11
    tiers = driven["tiers"]
    assert tiers["local"].path == tmp_path / "cache-local"
    assert tiers["shared"].path == tmp_path / "cache-shared"
    assert tiers["session"].path == tmp_path / "cache-session"
    assert tiers["project"].path == tmp_path / "cache-project"


def test_the_container_composition_carries_the_same_values(model, tmp_path, monkeypatch):
    """A container build unpacks the SDK too, and pins its two environment
    packages out of the same directories — so both reach it as well."""
    created: dict[str, object] = {}
    driven: dict[str, object] = {}

    monkeypatch.setattr(
        buildmethods,
        "create_build_context",
        lambda device_model, **kwargs: created.update(kwargs),
    )
    monkeypatch.setattr(buildmethods, "lock_context", lambda directory: None)
    monkeypatch.setattr(buildmethods, "context_facts", lambda directory: {})
    monkeypatch.setattr(
        buildmethods.containerbuild,
        "prepare_environment",
        lambda *a, **k: (
            type(
                "Resolved",
                (),
                {
                    "pin": type("Pin", (), {"reference": "an-image"})(),
                    "zephyr": "",
                    "found_under": "",
                },
            )(),
            False,
        ),
    )
    monkeypatch.setattr(
        buildmethods.containerbuild,
        "run_locked_build",
        lambda context_dir, **kwargs: driven.update(kwargs),
    )

    buildmethods.compose_local_build(
        model,
        signing_pub="",
        sdk_sources=(tmp_path / "sdk",),
        work_root=tmp_path / "work",
        env={},
        options=BuildOptions(
            workspace_sources=(tmp_path / "workspaces",),
            tools_sources=(tmp_path / "tools",),
            sdk_max_bytes=11,
        ),
    )
    assert created["workspace_sources"] == (tmp_path / "workspaces",)
    assert created["tools_sources"] == (tmp_path / "tools",)
    assert created["sdk_max_bytes"] == 11
    assert driven["sdk_max_bytes"] == 11


class _Stop(Exception):
    """Raised by a stubbed acquisition: the arguments are the whole subject."""


def test_the_container_backend_is_configured_with_the_sdk_bound(tmp_path, monkeypatch) -> None:
    """The last hop of the container path: the backend that unpacks the SDK."""
    from mcuhome.workbench import containerbuild
    from mcuhome.workbench import orchestrator as lb

    captured: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config, docker=None):
            captured["config"] = config

        def run(self, **kwargs):
            return lb.LocalOutcome(
                action="build",
                context_id="sha256:" + "2" * 64,
                exit_code=0,
                status="success",
                successful=True,
                artifacts=(),
                out=tmp_path / "out",
            )

    monkeypatch.setattr(lb, "LocalBackend", FakeBackend)
    containerbuild.run_locked_build(
        tmp_path / "context",
        image="ghcr.io/mcu-home/x:1",
        sdk_sources=(),
        work_root=tmp_path / "work",
        env={},
        sdk_max_bytes=11,
    )
    assert captured["config"].sdk_max_bytes == 11


def test_the_subprocess_backend_acquires_the_sdk_under_the_configured_bound(
    tmp_path, monkeypatch
) -> None:
    """And the last hop of the subprocess path, which unpacks it itself."""
    seen: dict[str, object] = {}

    def fake_acquire_sdk(**kwargs):
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(subprocessbuild, "acquire_sdk", fake_acquire_sdk)
    monkeypatch.setattr(subprocessbuild, "refuse_patched_context", lambda *a, **k: None)
    monkeypatch.setattr(subprocessbuild, "check_environment", lambda *a, **k: None)
    monkeypatch.setattr(
        subprocessbuild,
        "read_context_manifest",
        lambda path: type(
            "Manifest",
            (),
            {
                "sdk": type("Sdk", (), {"version": "0.1.0", "sha256": "a" * 64})(),
                "build_environment": None,
                "compute_id": lambda self: "sha256:" + "0" * 64,
            },
        )(),
    )
    monkeypatch.setattr(subprocessbuild, "read_generator_chain", lambda path: ())
    monkeypatch.setattr(subprocessbuild, "format_generator_chain", lambda entries: "mcuhome 0.1")
    with pytest.raises(_Stop):
        subprocessbuild.run_locked_build(
            tmp_path / "context",
            environment=None,
            sdk_sources=(),
            work_root=tmp_path / "work",
            env={},
            sdk_max_bytes=11,
        )
    assert seen["max_bytes"] == 11


# --------------------------------------------------------------------------
# The cache tiers themselves
# --------------------------------------------------------------------------


def test_a_named_local_tier_beats_the_layout_under_the_cache_root(tmp_path) -> None:
    under_root = subprocessbuild.cache_tiers(ccache_dir=tmp_path / "root")
    assert under_root["local"].path == tmp_path / "root" / "cache-local"
    assert under_root["local"].writable

    named = subprocessbuild.cache_tiers(
        ccache_dir=tmp_path / "root", local_dir=tmp_path / "elsewhere"
    )
    assert named["local"].path == tmp_path / "elsewhere"


def test_a_local_tier_can_be_named_without_a_cache_root(tmp_path) -> None:
    tiers = subprocessbuild.cache_tiers(local_dir=tmp_path / "elsewhere")
    assert tiers["local"].path == tmp_path / "elsewhere"
    assert "shared" not in tiers


def test_a_named_shared_tier_is_read_only_and_only_when_it_exists(tmp_path) -> None:
    absent = subprocessbuild.cache_tiers(shared_ccache_dir=tmp_path / "nothing")
    assert "shared" not in absent
    (tmp_path / "shared").mkdir()
    tiers = subprocessbuild.cache_tiers(shared_ccache_dir=tmp_path / "shared")
    assert tiers["shared"].path == tmp_path / "shared"
    assert not tiers["shared"].writable
