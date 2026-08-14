# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 1: YAML parsing and ``!secret`` resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import line_of
from mcuhome.model.errors import ConfigError

from mcuhome.workbench.loader import FileRef, editing_yaml, load_config, load_yaml_file

CONFIG_WITH_SECRET = """\
device:
  name: bench-node
  friendly_name: !secret device_label
  board: nrf7002dk/nrf5340/cpuapp
"""


def test_secret_is_resolved(write_config) -> None:
    entry = write_config(CONFIG_WITH_SECRET, secrets="device_label: Bench Node\n")
    data = load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert data["device"]["friendly_name"] == "Bench Node"


def test_unknown_secret_points_at_the_tag(write_config) -> None:
    entry = write_config(CONFIG_WITH_SECRET, secrets="other_label: Nope\n")
    with pytest.raises(ConfigError) as caught:
        load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")

    error = caught.value
    assert error.message == 'There is no secret called "device_label" in main.yaml.'
    assert error.location.line == line_of(CONFIG_WITH_SECRET, "!secret")
    assert error.location.column == CONFIG_WITH_SECRET.splitlines()[2].index("!secret") + 1
    assert error.location.key == "device.friendly_name"
    assert "device_label: your-value-here" in (error.hint or "")
    assert "secrets currently defined: other_label" in (error.hint or "")


def test_missing_secrets_file_is_explained(write_config) -> None:
    entry = write_config(CONFIG_WITH_SECRET)
    with pytest.raises(ConfigError) as caught:
        load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert "there is no main.yaml to read it from" in caught.value.message
    assert caught.value.location.line == line_of(CONFIG_WITH_SECRET, "!secret")


def test_secrets_file_is_only_read_when_used(write_config) -> None:
    entry = write_config("device:\n  name: bench-node\n")
    # No secrets/main.yaml exists, and none is needed.
    data = load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert data["device"]["name"] == "bench-node"


def test_broken_yaml_reports_a_line(write_config) -> None:
    entry = write_config("device:\n  name: bench\n   board: oops\n")
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(entry)
    assert caught.value.message.startswith("This file is not valid YAML")
    assert caught.value.location.line is not None
    assert "indentation-sensitive" in (caught.value.hint or "")


def test_empty_configuration_is_rejected(write_config) -> None:
    entry = write_config("# nothing here\n")
    with pytest.raises(ConfigError) as caught:
        load_config(entry, secrets_file=entry.parent / "secrets" / "main.yaml")
    assert caught.value.message == "This device configuration is empty."


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(tmp_path / "gone.yaml")
    assert "does not exist" in caught.value.message


# --- !file: a value out of an external file (PO 2026-08-14) -----------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_file_ref_is_the_content_and_knows_its_file(tmp_path: Path) -> None:
    _write(tmp_path / "token.txt", "s3cret\n")
    entry = _write(tmp_path / "conf.yaml", "token: !file token.txt\n")
    value = load_yaml_file(entry)["token"]
    assert isinstance(value, FileRef)
    assert value == "s3cret\n"  # a plain str to every consumer
    assert isinstance(value, str)
    assert value.path == (tmp_path / "token.txt").resolve()
    assert value.path.is_absolute()
    assert value.raw == "token.txt"


def test_a_relative_reference_resolves_against_the_referencing_file(tmp_path: Path) -> None:
    _write(tmp_path / "sub" / "deep" / "key.pem", "MATERIAL")
    entry = _write(tmp_path / "sub" / "conf.yaml", "key: !file deep/key.pem\n")
    value = load_yaml_file(entry)["key"]
    assert value == "MATERIAL"
    assert value.path == (tmp_path / "sub" / "deep" / "key.pem").resolve()


def test_an_absolute_reference_is_taken_as_given(tmp_path: Path) -> None:
    target = _write(tmp_path / "elsewhere" / "key.pem", "MATERIAL")
    entry = _write(tmp_path / "conf.yaml", f"key: !file {target}\n")
    assert load_yaml_file(entry)["key"].path == target.resolve()


def test_the_path_is_real_even_through_a_symlink(tmp_path: Path) -> None:
    """`.path` is the realpath: an external tool gets the actual file."""
    real = _write(tmp_path / "real" / "key.pem", "MATERIAL")
    (tmp_path / "link.pem").symlink_to(real)
    entry = _write(tmp_path / "conf.yaml", "key: !file link.pem\n")
    assert load_yaml_file(entry)["key"].path == real.resolve()


def test_a_missing_referenced_file_stops_the_load_with_the_line(tmp_path: Path) -> None:
    """Eager and strict: the refusal comes at load time, located."""
    entry = _write(tmp_path / "conf.yaml", "# comment\nkey: !file gone.pem\n")
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(entry)
    assert '"gone.pem"' in caught.value.message
    assert "does not exist" in caught.value.message
    assert str((tmp_path / "gone.pem").resolve()) in caught.value.message
    assert caught.value.location.file == entry
    assert caught.value.location.line == 2


def test_an_empty_file_reference_is_refused(tmp_path: Path) -> None:
    entry = _write(tmp_path / "conf.yaml", "key: !file\n")
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(entry)
    assert "!file needs a file path" in caught.value.message


def test_a_tilde_reference_is_refused_with_the_reason(tmp_path: Path) -> None:
    entry = _write(tmp_path / "conf.yaml", "key: !file ~/key.pem\n")
    with pytest.raises(ConfigError) as caught:
        load_yaml_file(entry)
    assert "does not expand" in caught.value.message
    assert "answers for itself" in (caught.value.hint or "")


def test_editing_yaml_writes_the_reference_back_never_the_content(tmp_path: Path) -> None:
    """The round-trip trap: a plain dump would replace the pointer to a
    secret with the secret. `editing_yaml` keeps the reference."""
    _write(tmp_path / "key.pem", "THE-SECRET-MATERIAL")
    entry = _write(tmp_path / "conf.yaml", "# kept\nkey: !file key.pem\nother: value\n")
    data = load_yaml_file(entry)
    data["added"] = "later"
    import io

    buffer = io.StringIO()
    editing_yaml().dump(data, buffer)
    text = buffer.getvalue()
    assert "key: !file key.pem" in text
    assert "THE-SECRET-MATERIAL" not in text
    assert "# kept" in text
    assert "added: later" in text
