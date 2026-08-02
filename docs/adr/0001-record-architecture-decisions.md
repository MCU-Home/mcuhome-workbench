# 0001 — Record architecture decisions

- Status: accepted
- Date: 2026-08-02

## Context

MCUHome is designed in the open and will accumulate decisions whose
rationale would otherwise live only in chat logs and PR comments. The
project is run by a small team (product owner + AI engineering), which
makes explicit written decisions even more important.

## Decision

Every non-trivial design decision is recorded as a numbered ADR in
`docs/adr/`, in lightweight MADR style (Context / Decision / Consequences,
with a status field). Decisions the product owner has approved are marked
`accepted`; open questions are captured early as `proposed` or `deferred`
so they are visible.

## Consequences

- Rationale survives contributor turnover and AI session boundaries.
- PRs that change design direction must include or update an ADR.
- Slight process overhead per decision (intentionally small: one page max).
