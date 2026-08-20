# Contributing to MCUHome

Thank you for considering a contribution! This is the MCUHome **flagship**
repository — `mcuhome.workbench` (distribution `mcuhome-workbench`) and
the project-wide architecture decision records. Firmware/SDK
contributions (west manifest, C runtime, `mcuhome.model`/`mcuhome.compiler`)
go to [mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk)
instead — see its own CONTRIBUTING.md.

The most valuable contributions right now are discussion and review of
the [architecture decision records](docs/adr/) and participation in
[GitHub Discussions](https://github.com/mcu-home/mcuhome-workbench/discussions).

## Development environment

No west workspace here — this repository is plain Python. Clone
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) next to
it for the two packages the workbench builds on:

```sh
git clone https://github.com/mcu-home/mcuhome-workbench
git clone https://github.com/mcu-home/mcuhome-sdk
cd mcuhome
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../mcuhome-sdk/packaging/model \
            -e ../mcuhome-sdk/packaging/compiler \
            -e '.[remote]'
pre-commit install --install-hooks
pre-commit install --hook-type commit-msg
```

## Building and testing

```sh
pytest    # tests/python/ — the workbench test suite
```

The `remote` build method's own suite needs one more peer: clone
[mcu-home/mcuhome-buildserver](https://github.com/mcu-home/mcuhome-buildserver) next
to this repository and `pip install -e ../mcuhome-buildserver`. Without it,
`tests/python/test_sessionclient.py` skips itself with one reason naming
exactly what is missing (`pytest -rs`).

## Coding standards

- **Python:** `ruff` (lint + format), settings in `pyproject.toml`.
- **Licensing:** every new file needs SPDX headers (a
  `SPDX-FileCopyrightText` line and an `Apache-2.0` license identifier —
  copy them from any existing file; Markdown files are covered by
  `REUSE.toml`'s fallback annotation and need no inline header).
  **Never copy code from GPL-licensed projects — this explicitly includes
  ESPHome's C++ runtime.**

## Commit and PR rules

- **Conventional Commits:** `feat: …`, `fix: …`, `docs: …`, `chore: …` etc.
  Commit types drive automated releases (SemVer).
- **DCO sign-off:** every commit must be signed off (`git commit -s`),
  certifying the [Developer Certificate of Origin](https://developercertificate.org/).
  We use DCO instead of a CLA.
- Keep PRs focused; one logical change per PR.
- Non-trivial design decisions need an ADR draft in
  [docs/adr/draft/](docs/adr/draft/) — propose it in the PR. Drafts are
  living documents; the final ADR is written from the real result once
  the component is done ([docs/adr/README.md](docs/adr/README.md)). This
  is the flagship repo, so **project-wide decisions live here**;
  SDK-shaped decisions go to
  [mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) instead.

## Reporting issues

Use the [issue forms](https://github.com/mcu-home/mcuhome-workbench/issues/new/choose).
Security vulnerabilities go through [SECURITY.md](SECURITY.md), never public
issues.

## Code of Conduct

This project follows the [Contributor Covenant 3.0](CODE_OF_CONDUCT.md).
