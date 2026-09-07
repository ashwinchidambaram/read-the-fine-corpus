# Trust boundary: retrieved content is untrusted (M-068, §14.1)

> **This platform serves retrieval and cannot guarantee the safety of downstream
> generation. The caller owns that boundary.**

That sentence is the whole point of this page. It is not a footnote. If you are
building an agent or application on top of Read The Fine Corpus, read it and
design for it.

## What this means

RTFC ingests arbitrary documents and returns their contents to you. A document
can contain text addressed to a model — "ignore previous instructions,"
fabricated system messages, instructions to exfiltrate data. That text will be
embedded, retrieved, and handed back to you exactly as written. The platform does
**not** sanitize or rewrite it, because silent rewriting is both unreliable and
destructive of provenance (§14.1).

The platform will **not** launder untrusted content into trusted-looking content.
What it does instead:

- **Labels, does not sanitize.** Every retrieval result is marked as retrieved
  untrusted material.
- **Flags injection-shaped content.** Segments with imperative model-directed
  language, embedded role markers, or instruction-shaped content carry a
  suspicion score in provenance. The score is retrievable and filterable, and is
  **not** used to silently exclude content.
- **Flags invisible content.** White-on-white text, zero-size fonts, off-page
  positioning, and metadata-only text are detected and flagged at parse time.
- **States the trust level at the point of use.** The MCP tool description tells
  a consuming agent the material is untrusted.

## What the platform CANNOT do for you

RTFC cannot control what your application does with the chunks it returns. It
cannot guarantee that feeding a retrieved chunk into an LLM is safe. Prompt
injection lives in the *content*, and the content is the product. **Guarding your
generation step is your responsibility.**

## The signal you get: `trust_level`

Every `RetrievalResult` carries a `trust_level` field. Today it has one value:

```
trust_level: untrusted_ingested
```

All ingested content is `untrusted_ingested` — this is surfaced precisely so a
caller or agent knows the material is untrusted (see
[`docs/contracts/retrieval-response.md`](../contracts/retrieval-response.md)).
Alongside it, provenance carries the injection **suspicion score** and
invisible-content flags described above. Use them:

- Treat every retrieved chunk as **data, never as instruction**. Never
  concatenate retrieved text into a system prompt or an instruction slot.
- Keep retrieved content in a clearly delimited "context" region and instruct
  your model that everything inside it is untrusted reference material.
- Consider filtering or down-weighting on the suspicion score for high-stakes
  flows. (It is advisory, not a gate — the platform will not exclude for you.)
- Schema-validate anything a model produces from retrieved content rather than
  trusting free-form output.

## Where this boundary sits

RTFC is the retrieval half. You own the generation half. The platform gives you
honest labels and provenance so you can defend that half; it cannot defend it for
you.

## Related

- Spec §14.1 — Ingested content is untrusted input
- [`docs/contracts/retrieval-response.md`](../contracts/retrieval-response.md) — `trust_level` field
- [`docs/api/README.md`](../api/README.md) — API / MCP reference
