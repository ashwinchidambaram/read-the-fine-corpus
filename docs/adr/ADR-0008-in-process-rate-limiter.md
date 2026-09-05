# ADR-0008 — In-process token bucket per replica for retrieval rate limiting

Status: **accepted**.
Governing spec: §11.3 (scaling, per-tenant rate limits), §15 (fail-closed semantics).
Related: [contracts/retrieval_response.py](../../src/finecorpus/contracts/retrieval_response.py) (`ErrorCode.RATE_LIMITED`).

## Context

§11.3 requires per-tenant rate limits and quotas enforced at the retrieval service.  The
retrieval service is stateless and horizontally scalable (§11.3: "retrieval service stateless,
scaled by replica count behind a load balancer").

Rate limiting in distributed systems has two standard approaches:

1. **In-process per-replica limiter** — each replica maintains its own token bucket.  Simple,
   zero-infrastructure, but accuracy degrades with replica count because a tenant sees N
   independent buckets rather than one global counter.  A tenant running N replicas worth of
   concurrent requests can burst to N × limit before any single replica throttles it.
2. **Centralized limiter with shared state** — typically Redis (INCR + EXPIRE or Lua script).
   Accurate global fairness at the cost of introducing a new infrastructure dependency, an
   additional network hop on every query, and a new failure mode (Redis unavailable).

The platform operates in a phase where:
- Multi-replica retrieval deployments are not yet in production.
- No Redis dependency exists in the stack.
- The primary rate-limit concern is protecting the platform from a single tenant's runaway
  query load, not sub-percent fairness accuracy.

## Decision

**In-process token bucket per replica; no new infrastructure (no Redis).**

Each retrieval service replica maintains an in-process per-tenant token bucket.  Requests that
exceed the bucket drain rate receive `result_status=error` with `ErrorCode.RATE_LIMITED`
(`retriable=True`) and the HTTP layer returns 429 with a `Retry-After` header.

Bucket parameters are configurable per-KB from the ingestion config (quota field, Phase 4+);
the default bucket is a platform-wide ceiling to protect against runaway agents.

### Per-replica accuracy limitation (documented, not hidden)

With R replicas behind a round-robin load balancer, a tenant can submit up to R × rate_limit
requests per window before any single replica throttles it.  This is a known and accepted
inaccuracy for a stateless in-process design.

For the current single-replica deployment this limitation is moot.  For multi-replica
deployments callers and KB owners MUST understand that the effective burst headroom is
R × rate_limit, not rate_limit.

The limitation is surfaced:
- In this ADR (the authoritative record).
- In the `ErrorCode.RATE_LIMITED` docstring in `retrieval_response.py`.
- In the operator runbook (Phase 4 deliverable).

### Failure semantics

Rate limiting fails **open** for misconfigured or uninitialized buckets (i.e., if the bucket
cannot be read, the request proceeds rather than being blocked).  This is safe because the
platform's hard safety properties (tenant isolation, provenance completeness, fail-closed on
DB unavailability) do not depend on rate limiting.  Rate limiting is a resource-fairness
mechanism, not a security boundary.

## Revisit trigger

Adopt centralized rate limiting (Redis or equivalent) when **any** of the following is true:

- **Multi-replica retrieval deployment is in production** AND per-replica inaccuracy causes
  measurable tenant-fairness complaints or SLA breaches.  Rule of thumb: if the measured
  burst headroom (R × limit) is more than 3× the configured limit in normal operation,
  centralize.
- **A multi-tenant SLA contract requires global accuracy** — e.g., a paying tenant's contract
  specifies that they will not be throttled below their purchased quota under any circumstances.
  Per-replica accuracy cannot satisfy this because a slow replica may exhaust its bucket
  faster than others.
- **Redis is already in the stack for another reason** (e.g., caching, session state) — if the
  infrastructure cost is already paid, adopting it for rate limiting is low additional cost.

## Consequences

**Positive**
- Zero new infrastructure dependency.  The retrieval service remains stateless and deployable
  as a single container (§11.3).
- Zero additional latency per query (no network hop to an external counter service).
- Zero new failure mode: Redis unavailability cannot cause retrieval outages.
- Simple implementation: standard token-bucket algorithm in Python with a per-tenant dict
  protected by a threading.Lock or an asyncio.Lock.

**Negative / trade-offs**
- Per-replica inaccuracy (documented above).  Not a problem for single-replica deployments;
  becomes relevant at scale.
- State is lost on replica restart.  A restarted replica begins with full buckets, allowing a
  short post-restart burst.  Acceptable because the window is bounded by the replica startup
  time and the burst headroom is bounded by one bucket's worth.
- Does not support burst allowances that span replicas (e.g., a tenant that wants to draw down
  a weekly quota across a fleet).  Centralized state is required for cross-replica quota.

## Alternatives considered

1. **Redis INCR + EXPIRE (sliding window).**  *Not adopted* — introduces a new infrastructure
   dependency and a new failure mode for a problem that does not yet require global accuracy.
   Remains the recommended migration path when the revisit trigger is met.
2. **Redis Lua sliding window (token bucket in Redis).**  *Not adopted* for the same reason as
   option 1; more operationally complex than INCR + EXPIRE.
3. **Nginx/Envoy upstream rate limiting.**  *Not adopted* — operates at the HTTP-header level
   and cannot inspect tenant identity from JWT/API-key claims without a custom filter.  Moving
   rate limiting to the infrastructure layer would require either coarse IP-based limits (wrong
   granularity) or a custom Envoy/Nginx plugin (excessive complexity for a Phase 4 deliverable).
4. **No rate limiting in v1.**  *Not adopted* — §11.3 makes per-tenant rate limits a MUST.  An
   in-process limiter satisfies the MUST with minimal cost.
