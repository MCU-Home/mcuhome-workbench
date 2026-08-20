# 0005 — SemVer 0.x with Conventional Commits

- Status: draft
- Date: 2026-08-02

## Context

Two versioning conventions compete in this space: SemVer (Zephyr, most
tooling) and CalVer `YYYY.M.patch` (ESPHome, Home Assistant — familiar to
the target audience). Release automation (release-please, commit-driven
changelogs) works naturally with SemVer and Conventional Commits, but not
with CalVer. In the incubation phase there are no monthly releases to
justify CalVer anyway.

## Decision

- **SemVer, starting at 0.x**, for both repositories.
- **Conventional Commits** (`feat:`/`fix:`/`BREAKING CHANGE:` drive version
  bumps), enforced in CI on every commit of a push or pull request.
- **DCO sign-off** (`git commit -s`) on every commit from commit #1.
- Automated release PRs and changelog generation (release-please) once the
  first releasable artifact exists.

## Consequences

- Full release automation from day one of actual releases.
- Commit discipline is required from all contributors (enforced by hooks).
- **Revisit before 1.0:** whether to switch user-facing releases to CalVer
  for Home Assistant ecosystem familiarity. This ADR must be superseded
  explicitly if so.
