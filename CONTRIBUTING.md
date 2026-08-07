# Contributing to MCUHome

Thank you for considering a contribution! MCUHome is in its design phase, so
the most valuable contributions right now are discussion and review of the
[architecture decision records](docs/adr/) and participation in
[GitHub Discussions](https://github.com/mcu-home/mcuhome/discussions).

## Development environment

MCUHome is a Zephyr west workspace application (T2 topology). The workspace
top directory must **not** be a git repository:

```sh
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome
west init -l mcuhome
west update
```

Install the [Zephyr SDK](https://docs.zephyrproject.org/latest/develop/getting_started/index.html)
matching the Zephyr release pinned in [west.yml](west.yml).

For the Python builder package:

```sh
cd mcuhome
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
pre-commit install --install-hooks
pre-commit install --hook-type commit-msg
```

## Building and testing

```sh
west build -p -b native_sim mcuhome/app          # quick build check
west twister -T mcuhome/tests --integration      # test suites
```

## Coding standards

- **C:** Zephyr coding style, enforced by the repo's `.clang-format`
  (tabs, Linux-kernel-derived style). Prefer static allocation; no heap
  allocation after initialization; keep Sleepy End Device power budgets in
  mind (no busy-waiting, no unnecessary wakeups).
- **Python:** `ruff` (lint + format), settings in `pyproject.toml`.
- **Licensing:** every new file needs SPDX headers (a
  `SPDX-FileCopyrightText` line and an `Apache-2.0` license identifier —
  copy them from any existing file).
  **Never copy code from GPL-licensed projects — this explicitly includes
  ESPHome's C++ runtime.**

## Commit and PR rules

- **Conventional Commits:** `feat: …`, `fix: …`, `docs: …`, `chore: …` etc.
  Commit types drive automated releases (SemVer).
- **DCO sign-off:** every commit must be signed off (`git commit -s`),
  certifying the [Developer Certificate of Origin](https://developercertificate.org/).
  We use DCO instead of a CLA.
- Keep PRs focused; one logical change per PR.
- Non-trivial design decisions need an ADR in [docs/adr/](docs/adr/) —
  propose it in the PR.

## Reporting issues

Use the [issue forms](https://github.com/mcu-home/mcuhome/issues/new/choose).
Security vulnerabilities go through [SECURITY.md](SECURITY.md), never public
issues.

## Code of Conduct

This project follows the [Contributor Covenant 3.0](CODE_OF_CONDUCT.md).
