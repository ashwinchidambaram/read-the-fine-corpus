# ADR 0002: Reference Embedding Providers

**Status:** Accepted  
**Date:** 2026-09-03  
**Deciders:** Ashwin Chidambaram (product owner), build orchestrator

## Context

The specification (Section 4.6) mandates **provider parity:** cloud and local model providers must both be first-class experiences. Air-gapped operation with no external egress must remain fully supported, and cloud-hosted operation must not be a bolt-on afterthought. The first-run setup prompt must offer both models equally.

Selecting one reference cloud provider and one reference local provider establishes the baseline for test coverage, performance measurements, and the parity contract.

## Decision

- **Reference cloud provider:** OpenAI's `text-embedding-3` family (specifically `text-embedding-3-small` or `text-embedding-3-large`)
- **Reference local provider:** Ollama, running one of the common open-source embedding models (`nomic-embed-text` or `bge-m3`)

Both are configured as equally valid options in the first-run setup flow. Documentation, tests, and performance baselines cover both paths.

## Rationale

- **Ollama for local deployment:** Ollama is the most common local embedding infrastructure in self-hosted RAG deployments. Most engineering teams already operating Ollama can bring their own local model without additional learning. Ollama handles model lifecycle, batching, and inference straightforwardly. The ecosystem is mature and actively maintained.
- **OpenAI for cloud deployment:** OpenAI's embedding models are the current industry standard for production cloud-based embedding. Teams already using OpenAI for generation naturally extend to use their embeddings. The API is well-documented and reliable.
- **Widespread availability:** Both options are free or low-cost for testing, but both also support production deployments at scale. No user is locked into either path.
- **Mutual knowledge:** Self-hosters evaluating this product likely have direct experience or easy access to documentation for both providers. Choosing unfamiliar or niche options would create unnecessary onboarding friction.

## Consequences

- The integration test suite (Section 18.2, "golden-corpus parity tests") runs the full integration pipeline against both the OpenAI and Ollama providers.
- No feature may pass testing under one provider and fail under the other. Behavioral or performance differences must be documented and intentional.
- Configuration validation ensures the selected provider is available and working before ingestion begins.
- Performance baselines in documentation cover both providers separately to set realistic expectations.
- The CLI and UI default to offering both providers in the first-run setup, with no hidden preference toward cloud or local.

## Alternatives Considered

- **Google Vertex AI Embeddings:** Also mature and widely used, but less familiar to self-hosters and would require GCP credentials. Does not add unique value over OpenAI in the first-run experience.
- **Hugging Face Transformers with local inference:** More flexible for model choice but requires ML-specific infrastructure knowledge (CUDA, memory, vLLM). Higher bar for the average self-hoster. Ollama abstracts this complexity better.
- **Mistral Embeddings or other emerging cloud providers:** Newer and less battle-tested at production scale. First-run flow should offer proven, widely adopted options.
- **Multiple reference providers (e.g., three cloud, three local):** Increases test matrix complexity, documentation burden, and maintenance cost without commensurate user benefit. Two providers establish the parity contract well; additional providers can be added as adapters later.
