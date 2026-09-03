# API & MCP Reference

Status: placeholder — populated in Phase 1 (REST retrieval API, OpenAPI-documented) and
Phase 4 (MCP server, rate limits, service-principal auth).

Until then, the designed interface surface lives in:

- [Retrieval response contract](../contracts/retrieval-response.md) — the Serve→caller
  envelope, result-status taxonomy, explain-mode extension, trust labelling
- [Architecture overview](../architecture/overview.md) — the three interfaces (REST, MCP,
  Python client) and the tenant isolation mechanism
