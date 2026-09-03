# Subagent Delegation Model

This document specifies how implementation work is organized and delegated during the Read The Fine Corpus build.

## Delegation Structure

The build is **orchestrated centrally** by one agent (the orchestrator) who holds:

- The specification and all cross-cutting constraints
- The architecture and service boundaries
- The phase plan and sequencing
- The list of open decisions and ambiguities
- The integration responsibilities

**Implementation is delegated** to scoped subagents, each handling one well-defined unit of work. Subagents do not inherit the orchestrator's full context — they work from explicit briefs.

## Specialist Roles

| Role | Responsibility | Duration |
|---|---|---|
| **Implementer** | Write code for one module, contract, or fix following a brief. Do not make architectural decisions. Report what was built. | Per-brief |
| **Test author** | Write and validate tests for a module or contract. Ensure acceptance criteria pass. | Per-module |
| **Correctness reviewer** | Read the diff. Verify it satisfies the brief, matches the spec, and passes contracts. Independent from the implementer. | Per-PR |
| **Security reviewer** | Review code touching ingestion, retrieval, tenancy, secrets, deletion, or audit (spec §14 and §17). Mandatory for anything in those domains. Independent from author. | Per-PR |
| **Docs writer** | Write wiki pages and architecture documentation for completed work. | Per-phase |
| **Investigator** | Debug test failures or production issues; report findings without attempting fixes. | On-demand |

## Hard Rules

1. **A reviewer never reviews code it wrote.** Independent review is the whole point.
2. **The orchestrator reads diffs and test output, not summaries.** Subagent reports are evidence, not proof. Spot-check diffs against invariants (spec §8) and contracts (spec §12).
3. **A subagent resolving a spec ambiguity on its own initiative is a review finding.** Ambiguity resolution is the orchestrator's job. If a subagent makes a design call without a brief, it is flagged and re-reviewed.
4. **Briefs carry relevant spec MUSTs verbatim.** Copy the exact text from the spec so the subagent knows the constraints.
5. **No changes to CLAUDE.md or the spec.** Only the orchestrator updates these files.

## Model-Tier Policy

Distribute work by cognitive complexity to optimize token spend:

| Complexity | Model tier | Examples |
|---|---|---|
| **Mechanical / boilerplate** | Haiku | Migrations, fixtures, simple refactors, configuration files |
| **Scoped module and test drafting** | Sonnet | Implement a single module against a contract, write integration tests for it |
| **Contract-level, security-critical, architectural drafting** | Opus or orchestrator | Design a new data contract, security review of tenancy logic, service boundary decisions |
| **Arbitration and integration** | Orchestrator only | Conflict resolution, cross-cutting refactors, phase sign-off, acceptance-criteria verification |

**Local Ollama models are NOT used for build tasks.** As of 2026-09-03, local models are too weak for a production codebase. Ambiguities get resolved wrong. Briefs get misunderstood. Code quality degrades. Use cloud-hosted Haiku/Sonnet/Opus only. Ollama is a runtime embedding provider, not a build tool.

## Standard Brief Template

Use this format for every delegation. Fill in each field and make the boundaries explicit.

````markdown
## Brief: [task title]

**Task:** [One concern. One sentence.]

**Spec sections:**
- §X.Y MUST [exact quote]
- §X.Y "executor defines" [exact quote, if applicable]

**Contracts touched:**
- [module/interface/schema]
- [module/interface/schema]

**Tests that must pass:**
- `pytest tests/test_[module].py::[function]`
- [any integration test relevant to this task]

**Files you may change:**
- `src/[module]/...`
- `tests/test_[module].py`

**Files you may NOT touch:**
- `CLAUDE.md` (orchestrator only)
- `spec-read-the-fine-corpus.md` (orchestrator only)
- `docs/adr/` (orchestrator only)
- Any service not listed in "Files you may change"

**What NOT to do:**
- Do not resolve spec ambiguities. Flag them as comments.
- Do not change the service boundary.
- Do not add dependencies without checking with the orchestrator.
- [Add any other explicit don'ts relevant to this task]

**Report format:**
- What you built (files changed, behavior added)
- Test results (pass/fail, coverage if applicable)
- Any blockers or spec ambiguities you found
- Any assumptions you made
````

## Review Workflow

1. **Implementer** lands a PR with code, tests, and documentation.
2. **Correctness reviewer** reads the diff, runs tests locally, and checks against the brief and spec.
3. **Security reviewer** (if applicable) reviews code touching sensitive domains.
4. **Orchestrator** spot-checks the diff against invariants and makes a merge decision.
5. If review findings are raised, **implementer** fixes or explains; if a disagreement arises, **orchestrator** arbitrates.
6. PR is merged to main. Main is always releasable.

## Avoiding Context Spillover

Briefs are the contract between orchestrator and subagent. A brief that says "implement the ingestion pipeline" is a breach — it requires holding the entire architecture in mind, which defeats the delegation model.

Briefs that work:
- "Implement the Segment class and write its tests; the schema is in `[wiki/link]` and the invariants are in §8.3."
- "Fix the provider-switch test failure. The test is in `tests/test_embedding_provider.py`. It expects [exact assertion]. The embedding config is in `[wiki/link]`."
- "Write the data contract and schema for the Job object; see briefs/job-contract.md for the interface."

Briefs that don't:
- "Build the ingestion system."
- "Make sure everything passes the parity test."
- "Implement Phase 2." (The orchestrator does this, at orchestrator tempo.)
