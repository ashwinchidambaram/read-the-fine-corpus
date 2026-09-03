# ADR 0004: Design Documentation as Living Wiki

**Status:** Accepted  
**Date:** 2026-09-03  
**Deciders:** Ashwin Chidambaram (product owner), build orchestrator

## Context

The specification (build rule 3) states that the `/docs` wiki is the build's working memory. Architecture, contracts, configuration, and operational knowledge must be maintained as a living resource, updated in sync with behavior changes. The alternative — producing a separate, monolithic design document before build, then splitting it into wiki pages as work progresses — creates a documentation-code divergence from day one.

Most infrastructure products defer documentation to a post-launch cleanup phase. This is a known failure mode: code evolves, documentation becomes stale, and eventually the documentation is abandoned as too expensive to keep synchronized.

## Decision

The pre-implementation design document is authored directly as permanent `/docs` wiki pages, not as a separate monolithic file. Every design choice produces a corresponding wiki page from the start. The design-review pull request is the initial wiki state. Every later phase updates these same pages in the same commit as the behavior changes they describe.

## Rationale

- **Single source of truth:** The wiki is not a separate document that must be kept in sync with code. It is the code's documentation, updated together.
- **Design review covers the wiki:** The design-review process reviews both the architecture and the documentation expressing it. Incomplete documentation is a review finding.
- **Working memory:** As the codebase grows and context fills, the wiki serves as the team's external memory. Later phases refer to wiki pages rather than reconstructing decisions from code or commit logs.
- **Operational value:** Self-hosters use this wiki to understand their deployment, debug issues, and extend the system. Keeping it current is not optional.

## Consequences

- **Documentation is not a phase end task.** Every implementation PR that changes behavior must update the corresponding wiki pages. A PR is incomplete if it touches code but not docs.
- **Wiki pages are versioned with code.** The wiki is committed alongside code. Releases tag both together.
- **No separate design document.** All design work is recorded directly in wiki pages, organized by topic (architecture, contracts, configuration, operations, ADRs, troubleshooting). A reader can follow a topic from first principles.
- **Design review gates implementation.** The design-review PR must reach agreement on the architecture and the wiki structure before any implementation code lands. Changes to the design imply changes to the wiki, which are reviewed before merge.

## Alternatives Considered

- **Monolithic design document pre-review:** Produces a complete design before implementation, then splits it up as work proceeds. Creates immediate divergence — the document becomes stale as soon as implementation encounters real constraints. Requires either dedicating a person to keeping it in sync or accepting that it will drift.
- **Post-launch documentation push:** Common in software projects; almost universally fails. By the time launch is near, pressure is high to avoid "non-product" work. Documentation gets shortchanged, and teams rarely have the time or context to fix it afterward.
- **Documentation separate from code review:** Treating docs as a separate, optional layer removed from code review creates a natural incentive to skip it. Enforcing "docs in the same PR as behavior changes" keeps them together.
