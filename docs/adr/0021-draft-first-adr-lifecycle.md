# 0021 — Draft-first ADR lifecycle

- Status: accepted
- Date: 2026-08-14
- Supersedes: [0001](0001-record-architecture-decisions.md)

## Context

ADR 0001 made every non-trivial design decision a numbered, static
document. During initial development that model worked against itself:
decisions legitimately change while a component is being built, and
because the documents were treated as fixed, every change landed as an
appended amendment or erratum instead of as better text. The result was
the opposite of what an ADR is for — ADR 0019 at one point carried
seven amendment sections, and reconstructing the current decision meant
replaying a patch series by hand.

The root cause is a timing error, not a format error: the decisions
were finalized before the things they decide existed. A decision about
a component that is still being built is a hypothesis; forcing it into
a fixed document guarantees errata.

## Decision

ADRs keep everything ADR 0001 established — every non-trivial design
decision is recorded as a numbered ADR in `docs/adr/`, lightweight MADR
style (Context / Decision / Consequences, with a status), decisions are
product-owner-approved before implementation, and PRs that change
design direction must include or update an ADR — but the documents get
a two-stage lifecycle:

1. **Every ADR starts as a draft** in `docs/adr/draft/`, status
   `draft`. A draft is a **living document**: when the decision
   changes, the text changes. Drafts never carry amendment or erratum
   sections — git history is the changelog. Drafts may be split,
   merged, or deleted outright when their subject disappears. `draft`
   describes the document's maturity, not missing approval: the
   decisions in a draft are approved when they are recorded, exactly as
   before.

2. **A final ADR is written when the component is done.** Once the
   implementation the decision governs is complete and verified, the
   ADR is rewritten **from the real result** — the code is the
   authority, the draft is source material — and moved to `docs/adr/`,
   status `accepted`, with a `Finalized:` date.

3. **Final ADRs are immutable.** After finalization only the status
   line may change (`superseded by NNNN`). A change to a finalized
   decision is a new draft, which supersedes the old final when it is
   itself finalized. Amendments and errata no longer exist in this
   process.

4. **Numbers are drawn from one sequence at draft creation** and follow
   the document for life. One final may consolidate several drafts; it
   names the numbers it absorbs, and absorbed numbers are retired,
   never reused.

Process decisions like this one, which are implemented by the same
commit that records them, may be created directly as final — the draft
stage exists for decisions whose implementation lies ahead.

This is a project-wide decision: it applies to every MCU-Home
repository that keeps ADRs (today `mcuhome` and `dashboard`).

## Consequences

- A reader of `docs/adr/` sees only settled decisions that describe
  built reality; a reader of `docs/adr/draft/` knows explicitly that
  the text is current thinking, not a promise.
- The cost of changing a decision drops to editing prose — where it
  belongs during development — instead of maintaining an erratum trail.
- A document no longer shows its decision's history; git does. Anyone
  who needs the old wording reads the old revision.
- The discipline moves to the finalization moment: finalizing too early
  recreates the old problem, because fixing a final requires a
  superseding draft. Finalize on verified completion, not on optimism.
- Existing ADRs were migrated once, in the change that introduced this
  process: still-churning ones moved to `draft/`, completed but
  amendment-laden ones were consolidated into clean text (their
  `Finalized:` line marks that consolidation), and already superseded
  ones stayed untouched as history — including their old amendment
  sections, which are now themselves history. External notes that cite
  "ADR NNNN's amendment" therefore refer to text that now lives
  integrated in the consolidated document, with the layering in git.
